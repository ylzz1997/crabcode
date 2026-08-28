export interface ProjectPreset {
  id: string;
  kind: "project" | "document";
  path: string;
  name: string;
  directories: string[];
  is_default?: boolean;
  last_session_id: string | null;
  favorite_session_ids?: string[];
  session_preferences?: Record<string, SessionPreferences>;
  document_view?: DocumentViewState;
}

export interface SessionPreferences {
  // Runtime controls are not part of Gateway session metadata, so Desktop
  // keeps them alongside the project/session selector.
  model_profile?: string | null;
  reasoning_effort?: ReasoningEffort | null;
  ultra_mode?: boolean;
  mode?: "agent" | "plan";
  permission_mode?: string;
}

export interface DocumentViewState {
  zoom: number;
  scroll_top: number;
  scroll_left: number;
}

export interface FavoriteFolder {
  id: string;
  type: "folder";
  name: string;
  children: FavoriteEntry[];
}

export interface FavoriteProject {
  id: string;
  type: "project";
  project_id: string;
}

export interface FavoriteSession {
  id: string;
  type: "session";
  project_id: string;
  session_id: string;
}

export type FavoriteEntry = FavoriteFolder | FavoriteProject | FavoriteSession;

export interface ConnectionPreset {
  id: string;
  name: string;
  base_url: string;
  credential_ref: string | null;
  allow_insecure_remote: boolean;
  last_model_profile?: string | null;
  document_workspace_root: string | null;
  projects: ProjectPreset[];
  favorite_items?: FavoriteEntry[];
  last_project_path: string | null;
  last_project_id: string | null;
}

export interface DesktopSettings {
  schema_version: 4;
  active_connection_id: string;
  connection_order: string[];
  connections: ConnectionPreset[];
  python_path: string | null;
  sidebar_width: number;
  project_files_width: number;
  project_files_max_tabs: number;
  document_agent_width: number;
  document_agent_collapsed: boolean;
  document_show_original_text: boolean;
  document_translation_concurrency: number;
  document_translation_batch_size: number;
  theme_mode: ThemeMode;
  active_theme_id: string;
  custom_theme_presets: ThemePreset[];
  pointer_cursor: boolean;
  ui_font_size: number;
  code_font_size: number;
  diff_marker_style: DiffMarkerStyle;
  font_smoothing: boolean;
  show_turn_duration: boolean;
  turn_duration_format: TurnDurationFormat;
  composer_send_key: ComposerSendKey;
  file_upload_mode: FileUploadMode;
  file_upload_max_size_mb: number;
  dock_icon: DockIconChoice;
}

export type DockIconChoice = "dark" | "light" | "custom";
export type ThemeMode = "system" | "light" | "dark";
export type UiFontFamily = "system" | "inter" | "serif";
export type CodeFontFamily = "system-mono" | "menlo" | "monaco";
export type DiffMarkerStyle = "color" | "symbols";
export type TurnDurationFormat = "seconds" | "hms";
export type ComposerSendKey = "enter" | "mod_enter";
export type FileUploadMode = "content" | "path";

export interface ThemeProfile {
  accent_color: string;
  background_color: string;
  foreground_color: string;
  ui_font_family: UiFontFamily;
  code_font_family: CodeFontFamily;
  translucent_sidebar: boolean;
  contrast: number;
  radius_scale: number;
  shadow_strength: number;
  token_overrides: Partial<ThemeSemanticColors>;
}

export interface ThemeSemanticColors {
  bg: string;
  panel: string;
  panel_strong: string;
  surface: string;
  surface_hover: string;
  surface_active: string;
  border: string;
  border_soft: string;
  text: string;
  muted: string;
  subtle: string;
  accent: string;
  accent_strong: string;
  accent_soft: string;
  green: string;
  green_soft: string;
  orange: string;
  orange_soft: string;
  red: string;
  red_soft: string;
  code_bg: string;
}

export type ThemeVisualSlot =
  | "app_background"
  | "workspace_background"
  | "sidebar_overlay"
  | "welcome_character_left"
  | "welcome_character_right"
  | "composer_frame"
  | "top_trim"
  | "bottom_trim";

export type ThemeVisualFit = "cover" | "contain" | "fill" | "none";
export type ThemeVisualPosition =
  | "center"
  | "top"
  | "bottom"
  | "left"
  | "right"
  | "left top"
  | "left bottom"
  | "right top"
  | "right bottom";

export interface ThemeVisualAsset {
  data_url: string;
  opacity: number;
  fit: ThemeVisualFit;
  position: ThemeVisualPosition;
}

export type ThemeVisuals = Partial<Record<ThemeVisualSlot, ThemeVisualAsset>>;

export interface ThemePreset {
  id: string;
  name: string;
  author: string;
  version: string;
  description: string;
  minimum_app_version: string;
  light: ThemeProfile;
  dark: ThemeProfile;
  preview?: {
    light?: string;
    dark?: string;
  };
  visuals?: ThemeVisuals;
}

export interface WorkspaceInfo {
  startup_cwd: string;
  home: string;
  browse_roots: string[];
  documents_dir?: string;
}

export interface DocumentPreciseEngineStatus {
  available: boolean;
  status: "not_installed" | "downloading" | "installing" | "verifying" | "ready" | "broken" | "upgrade_required";
  version: string;
  installed_version?: string | null;
  install_root?: string;
  download_bytes?: number | null;
  download_estimated?: boolean;
  install_source?: "official" | "offline_bundle" | null;
  detail: string;
  install_command: string;
  remove_command: string;
}

export interface DocumentCapabilities {
  supported_extensions: string[];
  available_extensions: string[];
  max_bytes: number;
  documents_dir: string;
  libreoffice: { available: boolean; executable: string | null };
  ocr: { available: boolean };
  translation_engines?: {
    default: "legacy" | "precise";
    legacy: { available: true; status: "ready" };
    precise: DocumentPreciseEngineStatus;
  };
}

export interface DocumentManifest {
  schema_version: 1;
  project_id: string;
  project_name: string;
  workspace: string;
  source: {
    origin: "upload" | "url";
    name: string;
    path: string;
    url: string | null;
    content_type: string;
    size: number;
    sha256: string;
  };
  pdf: { path: string; sha256: string; page_count: number };
  layout: null | { path: string; fingerprint: string; text_pages: number; scanned_pages: number };
  translations: Record<string, {
    engine?: "legacy" | "precise";
    path?: string;
    content_path?: string;
    status?: string;
    source_sha256?: string;
    sha256?: string;
    pdf_sha256?: string;
    page_count?: number;
    engine_version?: string;
    warnings?: string[];
  }>;
  blog: null | { path: string; revision: string; language: string; operation_id?: string };
  jobs: Record<string, {
    action: "translate" | "generate_blog";
    status: string;
    locale: string;
    language?: string;
    source: string;
    current: number;
    total: number;
    message: string;
    engine?: "legacy" | "precise";
    updated_at: string;
  }>;
  created_at: string;
  updated_at: string;
}

export interface DocumentTextBlock {
  id: string;
  text: string;
  x: number;
  y: number;
  width: number;
  height: number;
  fontSize: number;
  fontFamily: string;
  direction: string;
  kind?: "text" | "formula" | "graphic";
  textAlign?: "left" | "center" | "right";
}

export interface DocumentPageLayout {
  width: number;
  height: number;
  blocks: DocumentTextBlock[];
  lines?: Array<{
    x: number;
    y: number;
    width: number;
    height: number;
  }>;
}

export interface DocumentLayout {
  fingerprint: string;
  page_count: number;
  pages: DocumentPageLayout[];
}

export type DocumentTranslation =
  | {
    engine: "legacy";
    locale: string;
    source_sha256?: string;
    layout_fingerprint: string;
    blocks: Array<{ id: string; translated_text: string }>;
  }
  | {
    engine: "precise";
    locale: string;
    source_sha256: string;
    pdf_sha256: string;
    page_count: number;
    engine_version: string;
    warnings: string[];
  };

export interface DocumentSelectionRect {
  page: number;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface DocumentAnnotation {
  id: string;
  label: string;
  note: string;
  text: string;
  rects: DocumentSelectionRect[];
  created_at: string;
  updated_at: string;
}

export interface DocumentReference {
  id: string;
  project_id: string;
  document_name: string;
  page_label: string;
  line_start?: number;
  line_end?: number;
  text: string;
}

export interface DocumentBlog {
  markdown: string;
  revision: string | null;
  language: string;
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
  files?: WorkspaceFileEntry[];
}

export interface WorkspaceFileEntry {
  name: string;
  path: string;
  size: number;
  hidden: boolean;
  is_symlink: boolean;
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
  forked_from_session_id?: string | null;
  forked_from_message_uuid?: string | null;
  forked_from_title?: string | null;
}

export type ReasoningEffort = "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max";

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
  reasoning_effort?: ReasoningEffort | null;
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
  group?: string;
}

export interface ModelSettingsEntry {
  name: string;
  group: string | null;
  is_default: boolean;
  configured: Record<string, unknown>;
  effective: Record<string, unknown>;
  overridden_fields: string[];
  sources: string[];
}

export interface ModelSettingsResponse {
  cwd: string;
  default_model: string | null;
  sources: string[];
  groups: Record<string, Record<string, unknown>>;
  models: ModelSettingsEntry[];
  warnings: string[];
  editable_sources?: ModelSettingsSource[];
}

export type ModelSettingsMutationAction =
  | "upsert_model"
  | "delete_model"
  | "upsert_group"
  | "delete_group"
  | "set_default_model"
  | "clear_default_model";

export interface ModelSettingsSource {
  id: "userSettings" | "projectSettings" | "localSettings";
  label: string;
  path: string;
  exists: boolean;
  writable: boolean;
}

export interface ModelSettingsMutation {
  action: ModelSettingsMutationAction;
  source: ModelSettingsSource["id"];
  cwd?: string;
  name?: string;
  previous_name?: string;
  config?: Record<string, unknown>;
  remove_fields?: string[];
}

export interface GoalInfo {
  objective: string;
  status: "active" | "paused" | "complete" | "blocked";
  token_budget: number | null;
  tokens_used: number;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface GoalState {
  goal: GoalInfo | null;
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
  running: boolean;
}

export interface BackgroundTaskInfo {
  task_id: string;
  agent_id: string | null;
  session_id: string;
  cwd?: string;
  description: string;
  task_type: string;
  source: string;
  status: string;
  output_file: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string;
  error: string;
  exit_code: number | null;
}

export interface CheckpointInfo {
  id: string;
  label?: string;
  timestamp?: string;
  files?: string[];
  snapshot_id?: string | null;
}

export type ChatItemKind =
  | "user"
  | "assistant"
  | "thinking"
  | "tool"
  | "permission"
  | "choice"
  | "plan"
  | "document_job"
  | "file_change"
  | "turn_duration"
  | "command"
  | "system"
  | "error";

export interface ImageAttachment {
  media_type: string;
  data: string;
}

export interface ChatItem {
  id: string;
  kind: ChatItemKind;
  text?: string;
  images?: ImageAttachment[];
  title?: string;
  command?: string;
  detail?: unknown;
  input?: Record<string, unknown>;
  result?: string;
  isError?: boolean;
  status?: "pending" | "running" | "retrying" | "complete" | "failed" | "allowed" | "denied" | "cancelled";
  tool_use_id?: string;
  agent_id?: string | null;
  options?: string[];
  multiple?: boolean;
  question?: string;
  selected?: string[];
  diff?: string | null;
  path?: string;
  action?: string;
  locale?: string;
  language?: string;
  source?: string;
  engine?: "legacy" | "precise";
  current?: number;
  total?: number;
  collapsed?: boolean;
  startedAt?: number;
  completedAt?: number;
  durationMs?: number;
}

export interface SessionCurrentStep {
  kind: "response" | "thinking" | "tool" | "permission" | "choice" | "document";
  label: string;
  startedAt: number;
}

export interface SessionViewState {
  id: string;
  cwd: string;
  title: string;
  items: ChatItem[];
  loading: boolean;
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
  images?: ImageAttachment[];
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
  status?: string;
  locale?: string;
  language?: string;
  source?: string;
  engine?: "legacy" | "precise";
  current?: number;
  total?: number;
  translated_text?: string;
  diff?: string | null;
  mode?: "agent" | "plan";
  model_profile?: string;
  permission_mode?: string;
  context_used_tokens?: number;
  context_window_tokens?: number;
  context_remaining_tokens?: number;
  context_used_percent?: number;
  assistant_message_uuid?: string | null;
  usage?: Record<string, unknown>;
  error_type?: string;
  recoverable?: boolean;
  command?: string;
  command_error?: boolean;
}
