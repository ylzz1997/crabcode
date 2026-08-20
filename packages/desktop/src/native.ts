import { invoke } from "@tauri-apps/api/core";
import type {
  CodeFontFamily,
  DesktopSettings,
  DiffMarkerStyle,
  DockIconChoice,
  ThemeMode,
  ThemeProfile,
  TurnDurationFormat,
  UiFontFamily,
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

const DEFAULT_LIGHT_THEME: ThemeProfile = {
  accent_color: "#e75f4b",
  background_color: "#f5f7f6",
  foreground_color: "#172421",
  ui_font_family: "system",
  code_font_family: "system-mono",
  translucent_sidebar: false,
  contrast: 50,
};

const DEFAULT_DARK_THEME: ThemeProfile = {
  accent_color: "#ff765f",
  background_color: "#0d1517",
  foreground_color: "#edf4ef",
  ui_font_family: "system",
  code_font_family: "system-mono",
  translucent_sidebar: false,
  contrast: 50,
};

const DEFAULT_SETTINGS: DesktopSettings = {
  schema_version: 2,
  active_connection_id: "local",
  connection_order: ["local"],
  connections: [{
    id: "local",
    name: "Local",
    base_url: "http://127.0.0.1:4096",
    credential_ref: null,
    allow_insecure_remote: false,
    last_model_profile: null,
    projects: [],
    last_project_path: null,
    last_project_id: null,
  }],
  python_path: null,
  sidebar_width: 280,
  theme_mode: "system",
  light_theme: DEFAULT_LIGHT_THEME,
  dark_theme: DEFAULT_DARK_THEME,
  pointer_cursor: true,
  ui_font_size: 14,
  code_font_size: 12,
  diff_marker_style: "color",
  font_smoothing: true,
  show_turn_duration: true,
  turn_duration_format: "hms",
  dock_icon: "dark",
};

function validHexColor(value: unknown): value is string {
  return typeof value === "string" && /^#[0-9a-f]{6}$/i.test(value);
}

function clampInteger(value: unknown, minimum: number, maximum: number, fallback: number): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return fallback;
  return Math.min(maximum, Math.max(minimum, Math.round(value)));
}

interface LegacyAppearanceSettings {
  auto_night_mode?: boolean;
  accent_color?: string;
  background_color?: string | null;
  foreground_color?: string | null;
  ui_font_family?: UiFontFamily;
  code_font_family?: CodeFontFamily;
  translucent_sidebar?: boolean;
  contrast?: number;
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
  return {
    ...raw,
    schema_version: 2,
    theme_mode: themeMode,
    light_theme: normalizeThemeProfile(raw.light_theme, DEFAULT_LIGHT_THEME, legacy),
    dark_theme: normalizeThemeProfile(raw.dark_theme, DEFAULT_DARK_THEME, legacy),
    pointer_cursor: raw.pointer_cursor !== false,
    ui_font_size: clampInteger(raw.ui_font_size, 11, 18, 14),
    code_font_size: clampInteger(raw.code_font_size, 10, 18, 12),
    diff_marker_style: diffMarkerStyle,
    font_smoothing: raw.font_smoothing !== false,
    show_turn_duration: raw.show_turn_duration !== false,
    turn_duration_format: turnDurationFormat,
    dock_icon: dockIcon,
    connections: (raw.connections ?? []).map((connection) => ({
      ...connection,
      last_model_profile: typeof connection.last_model_profile === "string"
        && connection.last_model_profile.trim().length > 0
        ? connection.last_model_profile
        : null,
      projects: (connection.projects ?? []).map((project) => {
        const legacyPath = typeof project.path === "string" ? project.path : "";
        const directories = Array.isArray(project.directories)
          ? project.directories.filter((path): path is string => typeof path === "string" && path.trim().length > 0)
          : legacyPath ? [legacyPath] : [];
        return {
          ...project,
          id: project.id || legacyPath || crypto.randomUUID(),
          path: directories[0] || legacyPath || "",
          directories,
          last_session_id: project.last_session_id ?? null,
          favorite_session_ids: Array.isArray(project.favorite_session_ids)
            ? [...new Set(project.favorite_session_ids.filter((id): id is string => typeof id === "string" && id.length > 0))]
            : [],
        };
      }),
      last_project_id: connection.last_project_id
        ?? connection.projects?.find((project) => project.path === connection.last_project_path)?.id
        ?? connection.last_project_path
        ?? null,
    })),
  };
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
