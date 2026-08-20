import { invoke } from "@tauri-apps/api/core";
import type { DesktopSettings } from "./types";

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
  schema_version: 2,
  active_connection_id: "local",
  connection_order: ["local"],
  connections: [{
    id: "local",
    name: "Local",
    base_url: "http://127.0.0.1:4096",
    credential_ref: null,
    allow_insecure_remote: false,
    projects: [],
    last_project_path: null,
    last_project_id: null,
  }],
  python_path: null,
  sidebar_width: 280,
};

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
  return {
    ...raw,
    schema_version: 2,
    connections: (raw.connections ?? []).map((connection) => ({
      ...connection,
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
        };
      }),
      last_project_id: connection.last_project_id
        ?? connection.projects?.find((project) => project.path === connection.last_project_path)?.id
        ?? connection.last_project_path
        ?? null,
    })),
  };
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
