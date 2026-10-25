"""Group discovery preserves credentials, routing, and the user's catalog."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crabcode_core.api import model_discovery as discovery
from crabcode_core.types.config import ApiConfig
from crabcode_gateway.routes import config as routes


def transport(monkeypatch, handler):
    client_class = httpx.AsyncClient
    options = []

    def client(**kwargs):
        options.append(dict(kwargs))
        kwargs.pop("proxy", None)  # MockTransport handles the wire, not a real proxy.
        return client_class(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(discovery.httpx, "AsyncClient", client)
    return options


@pytest.mark.parametrize("base,path", [
    ("https://example.test", "/v1/models"),
    ("https://example.test/v1/", "/v1/models"),
    ("https://example.test/proxy/v2", "/proxy/v2/models"),
])
def test_openai_compatible_catalog_and_headers(monkeypatch, base, path):
    monkeypatch.setenv("GROUP_KEY", "key-in-gateway")

    def handler(request):
        assert request.url.path == path
        assert request.headers["authorization"] == "Bearer header-override"
        assert request.headers["x-client"] == "crabcode"
        return httpx.Response(200, json={"data": [{"id": "z"}, {"id": "org/a"}, {"id": "z"}]})

    options = transport(monkeypatch, handler)
    result = asyncio.run(discovery.discover_models(ApiConfig(
        provider="router", format="codex", base_url=base, api_key_env="GROUP_KEY",
        network_mode="direct", http_headers={"Authorization": "Bearer header-override", "X-Client": "crabcode"},
    )))
    assert result == [{"id": "org/a"}, {"id": "z"}]
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False


def test_anthropic_auth_pagination_and_proxy(monkeypatch):
    monkeypatch.setenv("GROUP_KEY", "anthropic-secret")
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.path == "/prefix/v1/models"
        assert request.headers["x-api-key"] == "anthropic-secret"
        assert request.headers["anthropic-version"] == "2023-06-01"
        if len(seen) == 1:
            return httpx.Response(200, json={"data": [{"id": "one", "display_name": "One"}], "has_more": True, "last_id": "one"})
        assert request.url.params["after_id"] == "one"
        return httpx.Response(200, json={"data": [{"id": "two"}], "has_more": False})

    options = transport(monkeypatch, handler)
    result = asyncio.run(discovery.discover_models(ApiConfig(
        provider="anthropic", base_url="https://example.test/prefix", api_key_env="GROUP_KEY",
        network_mode="proxy", proxy_url="http://127.0.0.1:9000",
    )))
    assert result == [{"id": "one", "name": "One"}, {"id": "two"}]
    assert len(seen) == 2
    assert options[0]["trust_env"] is False
    assert options[0]["proxy"] == "http://127.0.0.1:9000"


@pytest.mark.parametrize("status", [301, 401, 403, 404, 429, 500])
def test_errors_do_not_leak_provider_content_or_retry(monkeypatch, status):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, text="secret-credential", headers={"location": "https://other.test/secret-credential"})

    transport(monkeypatch, handler)
    with pytest.raises(discovery.ModelDiscoveryError) as error:
        asyncio.run(discovery.discover_models(ApiConfig(provider="openai", base_url="https://example.test/v1")))
    assert str(status) in str(error.value)
    assert "secret-credential" not in str(error.value)
    assert len(seen) == 1


@pytest.mark.parametrize("payload", [{}, {"data": ["bad"]}, {"data": [{"id": ""}]}, {"data": [], "has_more": True}])
def test_invalid_catalog_is_not_treated_as_empty(monkeypatch, payload):
    transport(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(discovery.ModelDiscoveryError):
        asyncio.run(discovery.discover_models(ApiConfig(provider="openai")))


def test_empty_catalog(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, json={"data": []}))
    assert asyncio.run(discovery.discover_models(ApiConfig(provider="openai"))) == []


def test_non_json_response_is_reported_without_exposing_body(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, text="<html>secret-credential</html>"))
    with pytest.raises(discovery.ModelDiscoveryError, match="有效的模型列表") as error:
        asyncio.run(discovery.discover_models(ApiConfig(provider="openai")))
    assert "secret-credential" not in str(error.value)


def test_anthropic_bearer_auth_and_openai_project_headers():
    _, headers, _ = discovery._connection(ApiConfig(provider="anthropic"), {"ANTHROPIC_AUTH_TOKEN": "token"})
    assert headers["authorization"] == "Bearer token"
    _, headers, _ = discovery._connection(ApiConfig(provider="openai"), {"OPENAI_ORG_ID": "org", "OPENAI_PROJECT_ID": "project"})
    assert headers["OpenAI-Organization"] == "org"
    assert headers["OpenAI-Project"] == "project"


def test_settings_env_credentials_work_before_any_session_is_initialized(monkeypatch):
    monkeypatch.delenv("DISCOVERY_KEY", raising=False)
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"data": []})

    transport(monkeypatch, handler)
    config = ApiConfig(provider="openai", api_key_env="DISCOVERY_KEY")
    asyncio.run(discovery.discover_models(config, {"DISCOVERY_KEY": "from-settings"}))
    assert seen == ["Bearer from-settings"]
    assert "DISCOVERY_KEY" not in discovery.os.environ
    monkeypatch.setenv("DISCOVERY_KEY", "from-process")
    asyncio.run(discovery.discover_models(config, {"DISCOVERY_KEY": "from-settings"}))
    assert seen[-1] == "Bearer from-process"


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    home, project = tmp_path / "home", tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(routes, "_resolve_model_settings_cwd", lambda request, cwd: cwd)
    user_file = home / ".crabcode" / "settings.json"
    user_file.parent.mkdir()
    user_file.write_text(json.dumps({
        "groups": {"shared": {"provider": "openai", "base_url": "https://example.test/v1", "http_headers": {"Authorization": "Bearer group-secret"}, "network_mode": "direct"}},
        "models": {"existing": {"group": "shared", "model": "old-id", "max_tokens": 42}, "org-new": {"model": "other-id"}},
        "default_model": "existing",
    }))
    project_file = project / ".crabcode" / "settings.json"
    project_file.parent.mkdir()
    project_file.write_text(json.dumps({"groups": {"shared": {"timeout": 60}}, "unrelated": {"keep": True}}))
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        yield client, str(project), user_file, project_file


def test_discovery_uses_effective_group_and_never_writes(catalog, monkeypatch):
    client, cwd, user_file, project_file = catalog
    originals = [path.read_bytes() for path in (user_file, project_file)]
    probe = AsyncMock(return_value=[{"id": "new-id"}])
    monkeypatch.setattr(discovery, "discover_models", probe)
    response = client.post("/config/discover-models", json={"group": "shared", "cwd": cwd})
    assert response.status_code == 200
    assert response.json() == {"group": "shared", "models": [{"id": "new-id"}]}
    config = probe.call_args.args[0]
    assert config.http_headers == {"Authorization": "Bearer group-secret"}
    assert config.timeout == 60
    assert config.network_mode == "direct"
    assert "group-secret" not in response.text
    assert [path.read_bytes() for path in (user_file, project_file)] == originals
    assert client.post("/config/discover-models", json={"group": "missing", "cwd": cwd}).status_code == 404


def test_batch_import_skips_existing_and_preserves_layers(catalog):
    client, cwd, user_file, project_file = catalog
    user_before = user_file.read_bytes()
    body = {"action": "import_group_models", "source": "projectSettings", "cwd": cwd, "name": "shared", "model_ids": ["old-id", "org/new", "org/new", "fresh"]}
    response = client.post("/config/model-settings", json=body)
    assert response.status_code == 200
    value = json.loads(project_file.read_text())
    assert value["models"] == {"shared - org-new": {"group": "shared", "model": "org/new"}, "fresh": {"group": "shared", "model": "fresh"}}
    assert value["unrelated"] == {"keep": True}
    assert user_file.read_bytes() == user_before
    assert response.json()["default_model"] == "existing"
    imported = next(model for model in response.json()["models"] if model["name"] == "fresh")
    assert imported["effective"]["base_url"] == "https://example.test/v1"
    assert imported["effective"]["timeout"] == 60
    assert client.post("/config/model-settings", json=body).status_code == 200
    assert json.loads(project_file.read_text()) == value
    saved = project_file.read_bytes()
    assert client.post("/config/model-settings", json={**body, "name": "missing"}).status_code == 404
    assert client.post("/config/model-settings", json={**body, "model_ids": ["valid", "\n"]}).status_code == 422
    assert project_file.read_bytes() == saved
