"""Read a provider's model catalog using a saved group's connection policy."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx

from crabcode_core.api.network import certificate_failure, http_options
from crabcode_core.types.config import ApiConfig


class ModelDiscoveryError(ValueError):
    """A safe, user-facing discovery error (never a raw provider response)."""


def _connection(config: ApiConfig, env: Mapping[str, str]) -> tuple[str, httpx.Headers, bool]:
    provider = config.provider or "anthropic"
    protocol = (config.format or "openai") if provider == "router" else provider
    if protocol not in {"openai", "codex", "anthropic"}:
        raise ModelDiscoveryError("此配置组暂不支持获取模型列表，请手动添加模型")
    anthropic = protocol == "anthropic"
    key = env.get(config.api_key_env or "") or env.get(
        "ANTHROPIC_API_KEY" if anthropic else "OPENAI_API_KEY"
    )
    base = config.base_url or env.get(
        "ANTHROPIC_BASE_URL" if anthropic else "OPENAI_BASE_URL"
    )
    if protocol == "codex" and not config.base_url and not key:
        raise ModelDiscoveryError("Codex 登录认证暂不支持获取模型列表，请使用配置了 API 地址的组或手动添加")
    base = base or ("https://api.anthropic.com" if anthropic else "https://api.openai.com/v1")
    try:
        url = urlsplit(base.strip())
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError
        _ = url.port
    except ValueError:
        raise ModelDiscoveryError("请为配置组填写有效的 HTTP(S) Base URL（不含账号密码、查询参数或片段）") from None
    path = url.path.rstrip("/")
    if anthropic and not path.endswith("/v1"):
        path += "/v1"
    elif not path:
        path = "/v1"
    endpoint = urlunsplit((url.scheme, url.netloc, path + "/models", "", ""))
    headers = httpx.Headers({"accept": "application/json"})
    if anthropic:
        headers["anthropic-version"] = "2023-06-01"
        if key:
            headers["x-api-key"] = key
        elif env.get("ANTHROPIC_AUTH_TOKEN"):
            headers["authorization"] = f"Bearer {env['ANTHROPIC_AUTH_TOKEN']}"
    elif key:
        headers["authorization"] = f"Bearer {key}"
    if not anthropic:
        for variable, header in (("OPENAI_ORG_ID", "OpenAI-Organization"), ("OPENAI_PROJECT_ID", "OpenAI-Project")):
            if env.get(variable):
                headers[header] = env[variable]
    headers.update(config.http_headers)
    return endpoint, headers, anthropic


async def _fetch(config: ApiConfig, env: Mapping[str, str]) -> list[dict[str, str]]:
    endpoint, headers, anthropic = _connection(config, env)
    models: dict[str, dict[str, str]] = {}
    cursors: set[str] = set()
    params: dict[str, str | int] = {"limit": 1000} if anthropic else {}
    remaining_bytes = 4 * 1024 * 1024
    async with httpx.AsyncClient(
        **http_options(config), timeout=15, follow_redirects=False,
    ) as client:
        for _ in range(50):
            async with client.stream("GET", endpoint, headers=headers, params=params) as response:
                response.raise_for_status()
                chunks = []
                async for chunk in response.aiter_bytes():
                    remaining_bytes -= len(chunk)
                    if remaining_bytes < 0:
                        raise ModelDiscoveryError("模型列表过大，请缩小服务端返回范围后重试")
                    chunks.append(chunk)
                try:
                    payload = json.loads(b"".join(chunks))
                except (ValueError, UnicodeError):
                    raise ModelDiscoveryError("接口没有返回有效的模型列表，请检查 Base URL 是否指向 API") from None
            entries = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(entries, list):
                raise ModelDiscoveryError("接口没有返回有效的模型列表，请检查 Base URL 是否指向 API")
            for entry in entries:
                model_id = entry.get("id") if isinstance(entry, dict) else None
                if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 512 or any(ord(char) < 32 for char in model_id):
                    raise ModelDiscoveryError("模型列表包含无效的模型 ID，请检查服务端响应")
                model_id = model_id.strip()
                model = {"id": model_id}
                display_name = entry.get("display_name")
                if isinstance(display_name, str) and display_name.strip():
                    model["name"] = display_name.strip()[:512]
                models.setdefault(model_id, model)
                if len(models) > 5000:
                    raise ModelDiscoveryError("模型列表过大，请缩小服务端返回范围后重试")
            if not payload.get("has_more"):
                return sorted(models.values(), key=lambda model: model["id"].casefold())
            cursor = payload.get("last_id")
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ModelDiscoveryError("模型列表分页无效，请检查服务端响应")
            cursors.add(cursor)
            params["after_id" if anthropic else "after"] = cursor
    raise ModelDiscoveryError("模型列表分页过多，请缩小服务端返回范围后重试")


async def discover_models(config: ApiConfig, settings_env: Mapping[str, str] | None = None) -> list[dict[str, str]]:
    try:
        # Match session initialization's setdefault precedence without mutating
        # the Gateway environment or requiring an initialized chat session.
        env = {**(settings_env or {}), **os.environ}
        return await asyncio.wait_for(_fetch(config, env), timeout=15)
    except ModelDiscoveryError:
        raise
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        reason = {
            401: "认证失败，请检查配置组的 API Key 或认证请求头",
            403: "没有获取模型列表的权限，请检查密钥权限",
            404: "服务未提供模型列表接口，请检查 Base URL 或手动添加模型",
            405: "服务未提供模型列表接口，请手动添加模型",
            429: "请求过于频繁，请稍后重试",
        }.get(status, "获取模型列表失败，请检查服务状态与 API 地址")
        raise ModelDiscoveryError(f"HTTP {status}：{reason}") from None
    except (TimeoutError, httpx.TimeoutException):
        raise ModelDiscoveryError("获取模型列表超时（最多 15 秒），请检查网络和代理") from None
    except Exception as exc:
        if certificate_failure(exc):
            raise ModelDiscoveryError("证书校验失败，请检查证书或网络拦截") from None
        raise ModelDiscoveryError("获取模型列表失败，请检查配置组的 API 地址、认证信息和网络策略") from None
