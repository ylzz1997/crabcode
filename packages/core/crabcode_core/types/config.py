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
    provider: str | None = None  # anthropic | openai | codex | bedrock | vertex | router
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    format: str | None = None  # anthropic | openai | codex (for routers)
    max_tokens: int = 16384
    thinking_enabled: bool = True
    thinking_budget: int = 10000
    timeout: int = 300  # seconds, for API calls


class AgentSettings(BaseModel):
    """Settings for the built-in Agent (sub-agent) tool."""
    max_turns: int = 10
    timeout: int = 300
    max_output_chars: int = 12000
    stream_send_input_output: bool = False
    max_concurrency: int = 4
    max_depth: int = 2
    max_active_agents_per_run: int = 16
    types: dict[str, "AgentTypeConfig"] = Field(default_factory=dict)


class AgentTypeConfig(BaseModel):
    """Settings for a specific sub-agent type."""
    model_profile: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    prompt: str | None = None


class DisplaySettings(BaseModel):
    """Settings for tool result display in the terminal."""
    default_max_lines: int = 50
    tool_max_lines: dict[str, int] = {}
    max_chars: int = 50_000

    # Built-in defaults merged under tool_max_lines overrides
    _TOOL_DEFAULTS: dict[str, int] = {
        "Agent": 120,
        "Bash": 60,
        "Grep": 50,
        "Glob": 30,
        "Read": 80,
        "Lint": 60,
        "WebSearch": 50,
        "CodebaseSearch": 50,
    }

    def get_max_lines(self, tool_name: str) -> int:
        """Return max display lines for a tool, considering overrides."""
        if tool_name in self.tool_max_lines:
            return self.tool_max_lines[tool_name]
        return self._TOOL_DEFAULTS.get(tool_name, self.default_max_lines)


class LoggingSettings(BaseModel):
    """Settings for runtime logging."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "WARNING"
    file: str | None = None


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
    agent: AgentSettings = Field(default_factory=AgentSettings)
    display: DisplaySettings = Field(default_factory=DisplaySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    model_config = {"extra": "allow"}

    def get_api_config(self, model_name: str | None = None) -> ApiConfig:
        """Return the ApiConfig for a named model, falling back to the default api config."""
        name = model_name or self.default_model
        if name and name in self.models:
            return self.models[name]
        return self.api
