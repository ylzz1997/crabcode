"""Shared HTTP routing policy. Never fall back from a proxy to direct access."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import ssl
import sys
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar
from urllib.parse import urlsplit

import httpx


def http_options(config: Any) -> dict[str, Any]:
    mode = getattr(config, "network_mode", "inherit")
    if mode == "inherit":
        return {}
    if mode == "direct":
        return {"trust_env": False}
    proxy = getattr(config, "proxy_url", None)
    try:
        parsed = urlsplit(proxy or "")
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and parsed.port != 0
        valid = valid and not parsed.username and not parsed.password
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Proxy mode requires an HTTP(S) proxy URL without embedded credentials")
    return {"trust_env": False, "proxy": proxy}


def request_timeout(config: Any, sdk: Any = None, *, fallback: Any = None) -> Any:
    value = getattr(config, "timeout", None)
    timeout = float(value if value is not None else getattr(fallback, "timeout", 300))
    # SDK exports follow their supported transport (httpx or httpx2).
    timeout_type = sdk.Timeout if sdk is not None else httpx.Timeout
    return timeout_type(timeout, connect=min(timeout, 10.0))


def sdk_http_client(config: Any, sdk: Any = None) -> Any:
    client_type = sdk.DefaultAsyncHttpxClient if sdk is not None else httpx.AsyncClient
    return client_type(**http_options(config), timeout=request_timeout(config, sdk))


def sdk_options(config: Any, sdk: Any) -> dict[str, Any]:
    # Adapters own request retries; the query loop owns stream reconnects.
    # Disable SDK retries so they do not multiply those budgets.
    options: dict[str, Any] = {"max_retries": 0, "timeout": request_timeout(config, sdk)}
    routing = http_options(config)
    if routing:
        options["http_client"] = sdk_http_client(config, sdk)
    return options


_T = TypeVar("_T")


def request_error_is_retryable(exc: BaseException) -> bool:
    """Only transport failures and 5xx belong to the request retry budget."""
    if certificate_failure(exc):
        return False
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, httpx.TransportError):
            return True
        status = getattr(current, "status_code", None)
        if isinstance(status, int) and status >= 500:
            return True
        pending.extend(item for item in (current.__cause__, current.__context__) if item is not None)
    return False


def request_retry_limit(config: Any) -> int:
    from crabcode_core.query.retry import MAX_REQUEST_RETRIES
    return min(max(0, int(getattr(config, "request_max_retries", 4))), MAX_REQUEST_RETRIES)


async def retry_request(config: Any, request: Callable[[], Awaitable[_T]]) -> _T:
    """Retry opening a request, never reading/replaying a started stream."""
    from crabcode_core.query.retry import request_retry_backoff
    limit = request_retry_limit(config)
    for attempt in range(limit + 1):
        try:
            return await request()
        except Exception as exc:
            if attempt >= limit or not request_error_is_retryable(exc):
                raise
            await asyncio.sleep(request_retry_backoff(attempt + 1))
    raise RuntimeError("unreachable request retry state")


@asynccontextmanager
async def retry_stream_context(config: Any, factory: Callable[[], Any]) -> AsyncIterator[Any]:
    """Open an SDK/HTTP stream with retries and always close its context."""
    from crabcode_core.query.retry import request_retry_backoff
    limit = request_retry_limit(config)
    for attempt in range(limit + 1):
        manager = factory()
        try:
            stream = await manager.__aenter__()
        except Exception as exc:
            if attempt >= limit or not request_error_is_retryable(exc):
                raise
        else:
            # Raw HTTP returns error responses; SDKs raise on context entry.
            # Return the final response intact for provider error diagnostics.
            status = getattr(stream, "status_code", 0)
            if not isinstance(status, int) or status < 500 or attempt >= limit:
                break
            await manager.__aexit__(None, None, None)
        await asyncio.sleep(request_retry_backoff(attempt + 1))
    try:
        yield stream
    except BaseException:
        if not await manager.__aexit__(*sys.exc_info()):
            raise
    else:
        await manager.__aexit__(None, None, None)


def certificate_failure(exc: BaseException) -> bool:
    seen: set[int] = set()
    pending = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(current):
            return True
        pending.extend(item for item in (current.__cause__, current.__context__) if item is not None)
    return False


def network_error_message(exc: BaseException, config: Any) -> str:
    if certificate_failure(exc):
        return "模型连接证书校验失败，请检查证书或网络拦截；未自动重试"
    mode = getattr(config, "network_mode", "inherit")
    route = {"inherit": "继承进程代理环境", "direct": "直连", "proxy": "指定代理"}.get(mode, mode)
    return f"模型连接中断（{type(exc).__name__}，{route}）。请检查网络或代理状态后重试；更改系统环境变量需重启 Gateway。"
