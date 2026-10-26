"""Shared HTTP routing policy. Never fall back from a proxy to direct access."""
from __future__ import annotations

import ssl
from typing import Any
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


def request_timeout(config: Any, sdk: Any = None) -> Any:
    timeout = float(getattr(config, "timeout", 300))
    # SDK exports follow their supported transport (httpx or httpx2).
    timeout_type = sdk.Timeout if sdk is not None else httpx.Timeout
    return timeout_type(timeout, connect=min(timeout, 10.0))


def sdk_http_client(config: Any, sdk: Any = None) -> Any:
    client_type = sdk.DefaultAsyncHttpxClient if sdk is not None else httpx.AsyncClient
    return client_type(**http_options(config), timeout=request_timeout(config, sdk))


def sdk_options(config: Any, sdk: Any) -> dict[str, Any]:
    # The query loop owns retry budgets; nested SDK retries hide progress.
    options: dict[str, Any] = {"max_retries": 0, "timeout": request_timeout(config, sdk)}
    routing = http_options(config)
    if routing:
        options["http_client"] = sdk_http_client(config, sdk)
    return options


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
