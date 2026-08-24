"""Configuration types for CrabCode settings."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


ReasoningEffort = Literal[
    "none", "minimal", "low", "medium", "high", "xhigh", "max"
]
REASONING_EFFORT_LEVELS: tuple[ReasoningEffort, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)


class PermissionRule(BaseModel):
    """A single permission rule for tool access control."""
    tool: str
    path: str | None = None
    command: str | None = None


class AiReviewSettings(BaseModel):
    """Settings for AI-assisted tool permission review."""

    model: str | None = None
    decisions: list[Literal["allow", "ask", "deny"]] = Field(
        default_factory=lambda: ["allow", "ask"]
    )
    fallback: Literal["allow", "ask", "deny"] = "ask"
    timeout: int = 30


class PermissionsSettings(BaseModel):
    allow: list[PermissionRule] = Field(default_factory=list)
    deny: list[PermissionRule] = Field(default_factory=list)
    ask: list[PermissionRule] = Field(default_factory=list)
    # None means "not explicitly configured". PermissionManager still falls
    # back to ask mode, while config layering can distinguish an omitted value
    # from an explicit override.
    default_mode: str | None = None
    additional_directories: list[str] = Field(default_factory=list)
    # Deprecated compatibility alias for default_mode="run_everything".
    run_everything: bool = False
    ai_review: AiReviewSettings = Field(default_factory=AiReviewSettings)


class McpServerConfig(BaseModel):
    command: list[str] | None = None
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    disabled: bool = False


class LspServerConfig(BaseModel):
    """Configuration for a single LSP server.

    Users can customize built-in LSP servers or add new ones in settings.json,
    following the same pattern as OpenCode's opencode.json LSP configuration.
    """

    command: list[str] = Field(default_factory=list)
    extensions: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    initialization: dict[str, Any] = Field(default_factory=dict)
    disabled: bool = False


class ApiConfig(BaseModel):
    """API backend configuration."""
    # Optional shared configuration entry.  This is metadata used while
    # loading ``CrabCodeSettings`` and is retained on the resolved config so
    # callers can still identify its source group.
    group: str | None = None
    provider: str | None = None  # anthropic | openai | codex | ollama | gemini | azure | bedrock | vertex | router
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    codex_auth_path: str | None = None
    http_headers: dict[str, str] = Field(default_factory=dict)
    format: str | None = None  # anthropic | openai | codex | ollama | gemini | azure (for routers)
    max_tokens: int = 16384
    thinking_enabled: bool = True
    thinking_budget: int = 10000
    pass_reasoning_content: bool = False
    anthropic_stream_transport: Literal["auto", "sdk", "httpx"] = "auto"
    reasoning_effort: ReasoningEffort | None = None
    timeout: int = 300  # seconds, for API calls
    max_retries: int = Field(default=5, ge=0)  # transient API reconnects per request
    context_window: int | None = None  # override auto-detected context window
    prompt_cache_key: str | None = None  # OpenAI Responses API prompt cache routing key
    prompt_cache_retention: Literal["in_memory", "24h"] | None = None
    extra_body: dict[str, Any] = Field(default_factory=dict)
    azure_endpoint: str | None = None  # Azure OpenAI endpoint URL
    azure_api_version: str | None = None  # Azure API version
    azure_deployment: str | None = None  # Azure deployment name (if model field is not used for this)


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
    enable_lsp: bool = True


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
        "Browser": 60,
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


class TeamSettings(BaseModel):
    """Settings for Agent Teams feature."""

    max_teammates: int = Field(default=8, gt=0)
    backpressure_queue_size: int = Field(default=100, gt=0)
    max_message_size_bytes: int = Field(default=10_000, gt=0)
    inbox_dir: str | None = None
    bridge_policy: str = "deny"  # deny | allow_tagged | allow_all


class CrossSessionSettings(BaseModel):
    """Settings for messaging between independent local sessions."""

    enabled: bool = True
    name: str | None = None
    inbound: Literal["auto", "accept", "hold", "refuse"] = "auto"
    registry_dir: str | None = None
    queue_size: int = Field(default=50, gt=0)
    max_message_size_bytes: int = Field(default=10_000, gt=0)
    connect_timeout_seconds: float = Field(default=3.0, gt=0)


class ScheduleSettings(BaseModel):
    """Settings for the Schedule (cron/interval/once) subsystem."""

    enabled: bool = True
    max_concurrent_jobs: int = 4
    default_timeout: int = 600  # seconds
    max_runs_per_job: int | None = None  # None = unlimited
    persist: bool = True  # persist jobs across restarts
    log_retention_days: int = 30


class GatewaySecuritySettings(BaseModel):
    """Authentication settings for the HTTP/WebSocket/gRPC gateway."""

    mode: Literal["none", "password", "publickey", "mixed"] = "none"
    # A plain password is accepted for local development; deployments should
    # prefer password_hash or the CLI/environment instead.
    password: str | None = None
    password_hash: str | None = None
    jwt_secret: str | None = None
    authorized_keys: str = "~/.ssh/authorized_keys"
    token_ttl_seconds: int = Field(default=900, gt=0, le=86_400)


class GatewayWorkspaceSettings(BaseModel):
    """Directories the Gateway may expose through workspace discovery APIs."""

    browse_roots: list[str] = Field(default_factory=list)


class GatewaySettings(BaseModel):
    security: GatewaySecuritySettings = Field(default_factory=GatewaySecuritySettings)
    workspace: GatewayWorkspaceSettings = Field(default_factory=GatewayWorkspaceSettings)


class CrabCodeSettings(BaseModel):
    """Full settings.json schema."""
    permissions: PermissionsSettings = Field(default_factory=PermissionsSettings)
    env: dict[str, str] = Field(default_factory=dict)
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    api: ApiConfig = Field(default_factory=ApiConfig)
    # Shared API settings inherited by named models.  A model's explicitly
    # configured fields take precedence over the referenced group.
    groups: dict[str, ApiConfig] = Field(default_factory=dict)
    models: dict[str, ApiConfig] = Field(default_factory=dict)
    default_model: str | None = None
    hooks: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    auto_compact_enabled: bool = True
    max_context_length: int | None = None
    language: str | None = None
    output_style: str | None = None
    prompt_profile: dict[str, Any] | None = None
    extra_tools: list[str] = Field(default_factory=list)
    ultra_mode: bool = False
    tool_call_timeout: float | None = None
    tool_settings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    team: TeamSettings = Field(default_factory=TeamSettings)
    cross_session: CrossSessionSettings = Field(default_factory=CrossSessionSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    display: DisplaySettings = Field(default_factory=DisplaySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    lsp: dict[str, LspServerConfig] | bool = Field(default_factory=dict)

    model_config = {"extra": "allow"}

    @model_validator(mode="after")
    def _resolve_model_groups(self) -> "CrabCodeSettings":
        """Expand group settings into named models after validation.

        Pydantic populates omitted fields with defaults, so using a normal
        dictionary merge would incorrectly override group values (for example
        an omitted ``thinking_enabled`` would look like ``True``).  The
        per-model ``model_fields_set`` records which fields the user actually
        supplied and lets us inherit only the omitted fields.
        """
        if not self.groups or not self.models:
            return self

        resolved: dict[str, ApiConfig] = {}
        for name, model_config in self.models.items():
            group_name = model_config.group
            group_config = self.groups.get(group_name) if group_name else None
            if group_config is None:
                # Keep unknown groups non-fatal so a typo does not discard all
                # otherwise valid settings; the model remains usable on its
                # own and callers can report the missing group if desired.
                resolved[name] = model_config
                continue

            inherited = group_config.model_copy(deep=True)
            for field_name in ApiConfig.model_fields:
                if field_name in model_config.model_fields_set:
                    setattr(
                        inherited,
                        field_name,
                        deepcopy(getattr(model_config, field_name)),
                    )
            resolved[name] = inherited

        self.models = resolved
        return self

    def get_api_config(self, model_name: str | None = None) -> ApiConfig:
        """Return the ApiConfig for a named model, falling back to the default api config."""
        name = model_name or self.default_model
        if name and name in self.models:
            return self.models[name]
        return self.api
