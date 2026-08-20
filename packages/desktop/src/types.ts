export interface ProjectPreset {
  path: string;
  name: string;
  last_session_id: string | null;
}

export interface ConnectionPreset {
  id: string;
  name: string;
  base_url: string;
  credential_ref: string | null;
  allow_insecure_remote: boolean;
  projects: ProjectPreset[];
  last_project_path: string | null;
}

export interface DesktopSettings {
  schema_version: 1;
  active_connection_id: string;
  connection_order: string[];
  connections: ConnectionPreset[];
  python_path: string | null;
  sidebar_width: number;
}

export interface WorkspaceInfo {
  startup_cwd: string;
  home: string;
  browse_roots: string[];
}

export interface WorkspaceDirectoryEntry {
  name: string;
  path: string;
  hidden: boolean;
  is_symlink: boolean;
}

export interface WorkspaceDirectoryListing {
  path: string;
  parent: string | null;
  directories: WorkspaceDirectoryEntry[];
}

export interface SessionInfo {
  session_id: string;
  message_count: number;
  model: string;
  provider: string;
  created_at: string;
  title: string;
  cwd: string;
  tokens_used: number;
  preview: string;
}

export interface SessionStatus {
  session_id: string;
  version?: string;
  cwd: string;
  initialized?: boolean;
  message_count?: number;
  model: string;
  model_profile?: string | null;
  provider: string;
  mode: "agent" | "plan";
  reasoning_effort?: string | null;
  ultra_mode?: boolean;
  permission_mode: string;
  context_used_tokens: number;
  context_window_tokens: number;
  context_remaining_tokens?: number;
  context_used_percent: number;
  compact_count?: number;
  auto_compact_enabled?: boolean;
  thinking_enabled?: boolean;
  max_tokens?: number;
  tool_count?: number | null;
  agent_total?: number;
  agent_active?: number;
  agent_failed?: number;
  agent_pending_callbacks?: number;
  agent_max_concurrency?: number;
  monitor_total?: number;
  monitor_active?: number;
  monitor_failed?: number;
  search_index?: {
    state: string;
    chunks?: number | null;
    files?: number | null;
    done?: number | null;
    total?: number | null;
  } | null;
}

export interface GatewayModel {
  name: string;
  description?: string;
}

export interface SkillInfo {
  name: string;
  description: string;
}

export interface ToolInfo {
  name: string;
  description: string;
  is_read_only: boolean;
  is_enabled: boolean;
}

export interface ScheduleJobInfo {
  id: string;
  name: string;
  prompt: string;
  schedule: string;
  schedule_type: "cron" | "interval" | "once";
  cwd: string | null;
  enabled: boolean;
  status: string;
  last_run: string | null;
  next_run: string | null;
  run_count: number;
  max_runs: number | null;
  created_at: string;
  session_id: string | null;
  description: string;
  tags: string[];
  timeout: number | null;
  model_profile: string | null;
  extra: Record<string, unknown>;
}

export interface CheckpointInfo {
  id: string;
  label?: string;
  timestamp?: string;
  files?: string[];
}

export type ChatItemKind =
  | "user"
  | "assistant"
  | "thinking"
  | "tool"
  | "permission"
  | "choice"
  | "plan"
  | "file_change"
  | "system"
  | "error";

export interface ChatItem {
  id: string;
  kind: ChatItemKind;
  text?: string;
  title?: string;
  detail?: unknown;
  status?: "pending" | "running" | "complete" | "allowed" | "denied" | "cancelled";
  tool_use_id?: string;
  agent_id?: string | null;
  options?: string[];
  multiple?: boolean;
  question?: string;
  selected?: string[];
  diff?: string | null;
  path?: string;
  action?: string;
  collapsed?: boolean;
  startedAt?: number;
  completedAt?: number;
  durationMs?: number;
}

export interface SessionCurrentStep {
  kind: "response" | "thinking" | "tool" | "permission" | "choice";
  label: string;
  startedAt: number;
}

export interface SessionViewState {
  id: string;
  cwd: string;
  title: string;
  items: ChatItem[];
  busy: boolean;
  connected: boolean;
  operationId: string | null;
  status: SessionStatus | null;
  error: string | null;
  runStartedAt?: number | null;
  currentStep?: SessionCurrentStep | null;
  lastTurnUsage?: Record<string, unknown> | null;
}

export interface GatewayViewState {
  status: "connecting" | "online" | "offline" | "error";
  error: string | null;
  token: string | null;
  tokenExpiresAt: number;
  workspace: WorkspaceInfo | null;
  sessionsByProject: Record<string, SessionInfo[]>;
  models: GatewayModel[];
  runningCount: number;
  pendingCount: number;
}

export interface GatewayEvent {
  type: string;
  session_id?: string;
  operation_id?: string;
  operation_scope?: string;
  properties?: Record<string, unknown>;
  messages?: Array<Record<string, unknown>>;
  text?: string;
  message?: string;
  tool_name?: string;
  tool_input?: Record<string, unknown>;
  tool_use_id?: string;
  result?: string;
  result_for_display?: string;
  is_error?: boolean;
  reason?: string;
  agent_id?: string | null;
  request_kind?: string;
  question?: string;
  options?: string[];
  multiple?: boolean;
  selected?: string[];
  allowed?: boolean;
  always_allow?: boolean;
  feedback?: string | null;
  plan?: Record<string, unknown>;
  path?: string;
  action?: string;
  diff?: string | null;
  mode?: "agent" | "plan";
  model_profile?: string;
  permission_mode?: string;
  context_used_tokens?: number;
  context_window_tokens?: number;
  context_remaining_tokens?: number;
  context_used_percent?: number;
  usage?: Record<string, unknown>;
  error_type?: string;
  recoverable?: boolean;
  command?: string;
  command_error?: boolean;
}
