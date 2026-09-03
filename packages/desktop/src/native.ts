import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { legacyFavoriteEntries, normalizeFavoriteEntries } from "./favorites";
import { projectPathKey } from "./pathUtils";
import {
  DEFAULT_DARK_THEME,
  DEFAULT_LIGHT_THEME,
  DEFAULT_THEME_ID,
  THEME_SEMANTIC_COLOR_KEYS,
  ThemeRegistry,
  isThemeColor,
  legacyThemePreset,
  normalizeStoredThemePreset,
  themeProfilesEqual,
} from "./theme";
import type {
  CodeFontFamily,
  ComposerSendKey,
  DesktopSettings,
  DiffMarkerStyle,
  DockIconChoice,
  DocumentPreciseEngineStatus,
  DocumentViewState,
  ThemeMode,
  ThemeProfile,
  ThemeSemanticColors,
  TurnDurationFormat,
  UiFontFamily,
  ReasoningEffort,
  SessionPreferences,
} from "./types";

interface AuthResult {
  access_token: string | null;
  expires_in: number;
  mode: string;
}

interface EnsureGatewayResult {
  ready: boolean;
  started_by_desktop: boolean;
  python: string | null;
  version: string | null;
  message: string;
}

const DEFAULT_SETTINGS: DesktopSettings = {
  schema_version: 4,
  active_connection_id: "local",
  connection_order: ["local"],
  connections: [{
    id: "local",
    name: "Local",
    base_url: "http://127.0.0.1:4096",
    credential_ref: null,
    allow_insecure_remote: false,
    last_model_profile: null,
    document_workspace_root: null,
    projects: [],
    favorite_items: [],
    last_project_path: null,
    last_project_id: null,
  }],
  python_path: null,
  sidebar_width: 280,
  project_files_width: 640,
  project_files_max_tabs: 5,
  document_agent_width: 400,
  document_agent_collapsed: false,
  document_show_original_text: false,
  document_translation_concurrency: 3,
  document_translation_batch_size: 200,
  theme_mode: "system",
  active_theme_id: DEFAULT_THEME_ID,
  custom_theme_presets: [],
  pointer_cursor: true,
  ui_font_size: 14,
  code_font_size: 12,
  diff_marker_style: "color",
  font_smoothing: true,
  show_turn_duration: true,
  turn_duration_format: "hms",
  composer_send_key: "enter",
  file_upload_mode: "content",
  file_upload_max_size_mb: 5,
  dock_icon: "dark",
};

function validHexColor(value: unknown): value is string {
  return typeof value === "string" && /^#[0-9a-f]{6}$/i.test(value);
}

function clampInteger(value: unknown, minimum: number, maximum: number, fallback: number): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return fallback;
  return Math.min(maximum, Math.max(minimum, Math.round(value)));
}

function clampNumber(value: unknown, minimum: number, maximum: number, fallback: number): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return fallback;
  return Math.min(maximum, Math.max(minimum, value));
}

function normalizeDocumentView(raw: Partial<DocumentViewState> | undefined): DocumentViewState | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  return {
    zoom: Math.round(clampNumber(raw.zoom, .6, 2.5, 1.2) * 10) / 10,
    scroll_top: clampNumber(raw.scroll_top, 0, Number.MAX_SAFE_INTEGER, 0),
    scroll_left: clampNumber(raw.scroll_left, 0, Number.MAX_SAFE_INTEGER, 0),
  };
}

const REASONING_EFFORTS = new Set<ReasoningEffort>([
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
]);

function normalizeSessionPreferences(raw: unknown): Record<string, SessionPreferences> {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
  const normalized: Record<string, SessionPreferences> = {};
  for (const [sessionId, value] of Object.entries(raw)) {
    if (!value || typeof value !== "object" || Array.isArray(value)) continue;
    const candidate = value as Record<string, unknown>;
    const preference: SessionPreferences = {};
    if (typeof candidate.model_profile === "string" && candidate.model_profile.trim()) {
      preference.model_profile = candidate.model_profile;
    }
    if (typeof candidate.reasoning_effort === "string" && REASONING_EFFORTS.has(candidate.reasoning_effort as ReasoningEffort)) {
      preference.reasoning_effort = candidate.reasoning_effort as ReasoningEffort;
    }
    if (typeof candidate.ultra_mode === "boolean") preference.ultra_mode = candidate.ultra_mode;
    if (candidate.mode === "agent" || candidate.mode === "plan") preference.mode = candidate.mode;
    if (typeof candidate.permission_mode === "string" && candidate.permission_mode.trim()) {
      preference.permission_mode = candidate.permission_mode;
    }
    if (Object.keys(preference).length > 0 && sessionId.trim()) normalized[sessionId] = preference;
  }
  return normalized;
}

interface LegacyAppearanceSettings {
  schema_version?: number;
  auto_night_mode?: boolean;
  accent_color?: string;
  background_color?: string | null;
  foreground_color?: string | null;
  ui_font_family?: UiFontFamily;
  code_font_family?: CodeFontFamily;
  translucent_sidebar?: boolean;
  contrast?: number;
  light_theme?: Partial<ThemeProfile>;
  dark_theme?: Partial<ThemeProfile>;
  active_theme_id?: unknown;
  custom_theme_presets?: unknown;
}

function normalizeThemeProfile(
  raw: Partial<ThemeProfile> | undefined,
  fallback: ThemeProfile,
  legacy: LegacyAppearanceSettings,
): ThemeProfile {
  const uiFontFamily: UiFontFamily = raw?.ui_font_family === "inter" || raw?.ui_font_family === "serif"
    ? raw.ui_font_family
    : legacy.ui_font_family === "inter" || legacy.ui_font_family === "serif"
      ? legacy.ui_font_family
      : fallback.ui_font_family;
  const codeFontFamily: CodeFontFamily = raw?.code_font_family === "menlo" || raw?.code_font_family === "monaco"
    ? raw.code_font_family
    : legacy.code_font_family === "menlo" || legacy.code_font_family === "monaco"
      ? legacy.code_font_family
      : fallback.code_font_family;
  const overrides: Partial<ThemeSemanticColors> = {};
  const rawOverrides = raw?.token_overrides;
  if (rawOverrides && typeof rawOverrides === "object" && !Array.isArray(rawOverrides)) {
    for (const key of THEME_SEMANTIC_COLOR_KEYS) {
      const value = rawOverrides[key];
      if (isThemeColor(value)) overrides[key] = value.toLowerCase();
    }
  }
  return {
    accent_color: validHexColor(raw?.accent_color)
      ? raw.accent_color.toLowerCase()
      : validHexColor(legacy.accent_color) ? legacy.accent_color.toLowerCase() : fallback.accent_color,
    background_color: validHexColor(raw?.background_color)
      ? raw.background_color.toLowerCase()
      : validHexColor(legacy.background_color) ? legacy.background_color.toLowerCase() : fallback.background_color,
    foreground_color: validHexColor(raw?.foreground_color)
      ? raw.foreground_color.toLowerCase()
      : validHexColor(legacy.foreground_color) ? legacy.foreground_color.toLowerCase() : fallback.foreground_color,
    ui_font_family: uiFontFamily,
    code_font_family: codeFontFamily,
    translucent_sidebar: raw?.translucent_sidebar ?? legacy.translucent_sidebar === true,
    contrast: clampInteger(raw?.contrast ?? legacy.contrast, 0, 100, fallback.contrast),
    radius_scale: clampNumber(raw?.radius_scale, 0.5, 1.75, fallback.radius_scale),
    shadow_strength: clampInteger(raw?.shadow_strength, 0, 100, fallback.shadow_strength),
    token_overrides: overrides,
  };
}

export function isDesktopShell(): boolean {
  return "__TAURI_INTERNALS__" in window;
}

export async function loadSettings(): Promise<DesktopSettings> {
  if (isDesktopShell()) return normalizeSettings(await invoke<DesktopSettings>("load_desktop_settings"));
  const raw = localStorage.getItem("crabcode.desktop.settings");
  if (!raw) return structuredClone(DEFAULT_SETTINGS);
  try {
    return normalizeSettings(JSON.parse(raw) as DesktopSettings);
  } catch {
    return structuredClone(DEFAULT_SETTINGS);
  }
}

export function normalizeSettings(raw: DesktopSettings): DesktopSettings {
  const legacy = raw as DesktopSettings & LegacyAppearanceSettings;
  const dockIcon: DockIconChoice = raw.dock_icon === "light" || raw.dock_icon === "custom"
    ? raw.dock_icon
    : "dark";
  const themeMode: ThemeMode = raw.theme_mode === "light" || raw.theme_mode === "dark"
    ? raw.theme_mode
    : raw.theme_mode === "system" ? "system" : legacy.auto_night_mode === false ? "light" : "system";
  const diffMarkerStyle: DiffMarkerStyle = raw.diff_marker_style === "symbols" ? "symbols" : "color";
  const turnDurationFormat: TurnDurationFormat = raw.turn_duration_format === "seconds" ? "seconds" : "hms";
  const composerSendKey: ComposerSendKey = raw.composer_send_key === "mod_enter" ? "mod_enter" : "enter";
  const legacyLight = normalizeThemeProfile(legacy.light_theme, DEFAULT_LIGHT_THEME, legacy);
  const legacyDark = normalizeThemeProfile(legacy.dark_theme, DEFAULT_DARK_THEME, legacy);
  const parsedStoredThemes = Array.isArray(legacy.custom_theme_presets)
    ? legacy.custom_theme_presets
        .map(normalizeStoredThemePreset)
        .filter((theme): theme is NonNullable<typeof theme> => theme !== null)
    : [];
  const storedThemeIds = new Set<string>();
  const storedThemes = parsedStoredThemes.filter((theme) => {
    if (storedThemeIds.has(theme.id)) return false;
    storedThemeIds.add(theme.id);
    return true;
  });
  const hasLegacyAppearance = legacy.schema_version !== 4 && (
    !themeProfilesEqual(legacyLight, DEFAULT_LIGHT_THEME)
    || !themeProfilesEqual(legacyDark, DEFAULT_DARK_THEME)
  );
  const customThemes = hasLegacyAppearance
    ? [legacyThemePreset(legacyLight, legacyDark), ...storedThemes.filter((theme) => theme.id !== "custom.migrated")]
    : storedThemes;
  const requestedThemeId = hasLegacyAppearance
    ? "custom.migrated"
    : typeof legacy.active_theme_id === "string" ? legacy.active_theme_id : DEFAULT_THEME_ID;
  const registry = new ThemeRegistry(customThemes);
  const activeThemeId = registry.get(requestedThemeId) ? requestedThemeId : DEFAULT_THEME_ID;
  const normalized = {
    ...raw,
    schema_version: 4,
    theme_mode: themeMode,
    active_theme_id: activeThemeId,
    custom_theme_presets: customThemes,
    pointer_cursor: raw.pointer_cursor !== false,
    ui_font_size: clampInteger(raw.ui_font_size, 11, 18, 14),
    code_font_size: clampInteger(raw.code_font_size, 10, 18, 12),
    diff_marker_style: diffMarkerStyle,
    font_smoothing: raw.font_smoothing !== false,
    show_turn_duration: raw.show_turn_duration !== false,
    turn_duration_format: turnDurationFormat,
    composer_send_key: composerSendKey,
    file_upload_mode: raw.file_upload_mode === "path" ? "path" : "content",
    file_upload_max_size_mb: clampInteger(raw.file_upload_max_size_mb, 1, 100, 5),
    dock_icon: dockIcon,
    project_files_width: clampInteger(raw.project_files_width, 480, 1_000, 640),
    project_files_max_tabs: clampInteger(raw.project_files_max_tabs, 1, 50, 5),
    document_agent_width: clampInteger(raw.document_agent_width, 320, 4_000, 400),
    document_agent_collapsed: raw.document_agent_collapsed === true,
    document_show_original_text: raw.document_show_original_text === true,
    document_translation_concurrency: clampInteger(raw.document_translation_concurrency, 1, 8, 3),
    document_translation_batch_size: clampInteger(raw.document_translation_batch_size, 10, 400, 200),
    connections: (raw.connections ?? []).map((connection) => {
      const projects = (connection.projects ?? []).map((project, index) => {
        const legacyPath = typeof project.path === "string" ? project.path : "";
        const directories = Array.isArray(project.directories)
          ? project.directories.filter((path): path is string => typeof path === "string" && path.trim().length > 0)
          : legacyPath ? [legacyPath] : [];
        return {
          ...project,
          kind: project.kind === "document" ? "document" as const : "project" as const,
          id: project.id || legacyPath || crypto.randomUUID(),
          path: directories[0] || legacyPath || "",
          directories,
          is_default: project.is_default === true || index === 0,
          last_session_id: project.last_session_id ?? null,
          favorite_session_ids: Array.isArray(project.favorite_session_ids)
            ? [...new Set(project.favorite_session_ids.filter((id): id is string => typeof id === "string" && id.length > 0))]
            : [],
          session_preferences: normalizeSessionPreferences(project.session_preferences),
          document_view: normalizeDocumentView(project.document_view),
        };
      });
      const favoriteItems = Array.isArray(connection.favorite_items)
        ? normalizeFavoriteEntries(connection.favorite_items)
        : legacyFavoriteEntries({ projects });
      return {
        ...connection,
        document_workspace_root: typeof connection.document_workspace_root === "string"
          && connection.document_workspace_root.trim().length > 0
          ? connection.document_workspace_root
          : null,
        last_model_profile: typeof connection.last_model_profile === "string"
          && connection.last_model_profile.trim().length > 0
          ? connection.last_model_profile
          : null,
        projects,
        favorite_items: favoriteItems,
        last_project_id: connection.last_project_id
          ?? projects.find((project) => typeof connection.last_project_path === "string"
            && projectPathKey(project.path) === projectPathKey(connection.last_project_path))?.id
          ?? connection.last_project_path
          ?? null,
      };
    }),
  };
  const withoutLegacyAppearance = normalized as DesktopSettings & Record<string, unknown>;
  for (const key of [
    "auto_night_mode",
    "accent_color",
    "background_color",
    "foreground_color",
    "ui_font_family",
    "code_font_family",
    "translucent_sidebar",
    "contrast",
    "light_theme",
    "dark_theme",
    "project_files_open",
  ]) delete withoutLegacyAppearance[key];
  return withoutLegacyAppearance;
}

export async function setDockIcon(choice: DockIconChoice, pngBytes?: Uint8Array): Promise<void> {
  if (!isDesktopShell()) return;
  await invoke("set_dock_icon", {
    choice,
    pngBytes: pngBytes ? Array.from(pngBytes) : null,
  });
}

export async function loadCustomDockIcon(): Promise<Uint8Array | null> {
  if (!isDesktopShell()) return null;
  const bytes = await invoke<number[] | null>("load_custom_dock_icon");
  return bytes ? new Uint8Array(bytes) : null;
}

export async function saveSettings(settings: DesktopSettings): Promise<void> {
  if (isDesktopShell()) {
    await invoke("save_desktop_settings", { settings });
    return;
  }
  localStorage.setItem("crabcode.desktop.settings", JSON.stringify(settings));
}

export async function saveThemeExport(filename: string, bytes: Uint8Array): Promise<string | null> {
  if (!isDesktopShell()) return null;
  return invoke<string>("save_theme_export", { filename, bytes: Array.from(bytes) });
}

export async function storeCredential(reference: string, password: string): Promise<void> {
  if (isDesktopShell()) {
    await invoke("store_credential", { credentialRef: reference, password });
    return;
  }
  sessionStorage.setItem(`crabcode.credential.${reference}`, password);
}

export async function deleteCredential(reference: string): Promise<void> {
  if (isDesktopShell()) {
    await invoke("delete_credential", { credentialRef: reference });
    return;
  }
  sessionStorage.removeItem(`crabcode.credential.${reference}`);
}

export async function authenticateConnection(
  baseUrl: string,
  credentialRef: string | null,
): Promise<AuthResult> {
  if (isDesktopShell()) {
    return invoke<AuthResult>("authenticate_connection", {
      baseUrl,
      credentialRef,
    });
  }
  const infoResponse = await fetch(new URL("auth/info", normalizeBaseUrl(baseUrl)));
  if (!infoResponse.ok) throw new Error(`认证信息请求失败 (${infoResponse.status})`);
  const info = await infoResponse.json() as { mode: string; methods: string[] };
  if (info.mode === "none") return { access_token: null, expires_in: 0, mode: "none" };
  if (!credentialRef) throw new Error("此 Gateway 需要密码");
  const password = sessionStorage.getItem(`crabcode.credential.${credentialRef}`);
  if (!password) throw new Error("当前浏览器标签页没有保存密码，请重新编辑连接");
  const response = await fetch(new URL("auth/token", normalizeBaseUrl(baseUrl)), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ grant_type: "password", password }),
  });
  if (!response.ok) throw new Error(`Gateway 拒绝了密码 (${response.status})`);
  const token = await response.json() as { access_token: string; expires_in: number };
  return { ...token, mode: info.mode };
}

export async function ensureLocalGateway(
  connectionId: string,
  baseUrl: string,
  pythonPath: string | null,
  credentialRef: string | null,
): Promise<EnsureGatewayResult> {
  if (!isDesktopShell()) {
    return {
      ready: false,
      started_by_desktop: false,
      python: null,
      version: null,
      message: "浏览器版不会自动启动 Gateway",
    };
  }
  return invoke<EnsureGatewayResult>("ensure_local_gateway", {
    connectionId,
    baseUrl,
    pythonPath,
    credentialRef,
  });
}

export async function shutdownGateway(connectionId: string): Promise<boolean> {
  if (!isDesktopShell()) return false;
  return invoke<boolean>("shutdown_gateway", { connectionId });
}

export async function installDocumentEngine(
  pythonPath: string | null,
  bundle: string | null = null,
  onProgress?: (progress: DocumentEngineInstallProgress) => void,
): Promise<Record<string, unknown>> {
  if (!isDesktopShell()) throw new Error("高精度 PDF 引擎只能由桌面应用安装");
  const operationId = crypto.randomUUID();
  const unlisten = onProgress
    ? await listen<DocumentEngineInstallProgress>("document-engine-install-progress", (event) => {
        if (event.payload.operationId === operationId) onProgress(event.payload);
      })
    : null;
  try {
    return await invoke<Record<string, unknown>>("install_document_engine", {
      pythonPath,
      bundle,
      operationId,
    });
  } finally {
    unlisten?.();
  }
}

export interface DocumentEngineInstallProgress {
  operationId: string;
  stage: string;
  detail: string;
  percent: number;
}

export async function getDocumentEngineStatus(
  pythonPath: string | null,
): Promise<DocumentPreciseEngineStatus> {
  if (!isDesktopShell()) throw new Error("高精度 PDF 引擎状态只能在桌面应用中读取");
  return invoke<DocumentPreciseEngineStatus>("document_engine_status", { pythonPath });
}

export async function removeDocumentEngine(pythonPath: string | null): Promise<Record<string, unknown>> {
  if (!isDesktopShell()) throw new Error("高精度 PDF 引擎只能由桌面应用删除");
  return invoke<Record<string, unknown>>("remove_document_engine", { pythonPath });
}

export function normalizeBaseUrl(raw: string): string {
  const url = new URL(raw.trim());
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("Gateway 地址必须使用 http:// 或 https://");
  }
  url.search = "";
  url.hash = "";
  url.pathname = `${url.pathname.replace(/\/+$/, "")}/`;
  return url.toString();
}

export function isLoopbackUrl(raw: string): boolean {
  try {
    const host = new URL(raw).hostname.replace(/^\[|\]$/g, "").toLowerCase();
    return host === "localhost" || host === "::1" || host.startsWith("127.");
  } catch {
    return false;
  }
}

export function isInsecureRemoteUrl(raw: string): boolean {
  try {
    const url = new URL(raw);
    return url.protocol === "http:" && !isLoopbackUrl(raw);
  } catch {
    return false;
  }
}
