"""Configuration types for CrabCode settings."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class PermissionRule(BaseModel):
    """A single permission rule for tool access control."""
    tool: str
    path: str | None = None
    command: str | None = None


class PermissionsSettings(BaseModel):
    allow: list[PermissionRule] = Field(default_factory=list)
    deny: list[PermissionRule] = Field(default_factory=list)
    ask: list[PermissionRule] = Field(default_factory=list)
    default_mode: str | None = None
    additional_directories: list[str] = Field(default_factory=list)
    run_everything: bool = False


class McpServerConfig(BaseModel):
    command: list[str] | None = None
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    disabled: bool = False


class ApiConfig(BaseModel):
    """API backend configuration."""
    provider: str | None = None  # anthropic | openai | bedrock | vertex | router
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    format: str | None = None  # anthropic | openai (for routers)
    max_tokens: int = 16384
    thinking_enabled: bool = True
    thinking_budget: int = 10000
    timeout: int = 300  # seconds, for API calls


class CrabCodeSettings(BaseModel):
    """Full settings.json schema."""
    permissions: PermissionsSettings = Field(default_factory=PermissionsSettings)
    env: dict[str, str] = Field(default_factory=dict)
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    api: ApiConfig = Field(default_factory=ApiConfig)
    models: dict[str, ApiConfig] = Field(default_factory=dict)
    default_model: str | None = None
    hooks: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    auto_compact_enabled: bool = True
    max_context_length: int | None = None
    language: str | None = None
    output_style: str | None = None
    prompt_profile: dict[str, Any] | None = None
    extra_tools: list[str] = Field(default_factory=list)
    tool_settings: dict[str, dict[str, Any]] = Field(default_factory=dict)

    model_config = {"extra": "allow"}

    def get_api_config(self, model_name: str | None = None) -> ApiConfig:
        """Return the ApiConfig for a named model, falling back to the default api config."""
        name = model_name or self.default_model
        if name and name in self.models:
            return self.models[name]
        return self.api
