import { isDesktopShell } from "./native";

export type SessionNotifyPhase = "start" | "complete";

export interface SessionNotifyState {
  title: string;
  busy: boolean;
}

export interface SessionNotifyIntent {
  sessionId: string;
  phase: SessionNotifyPhase;
  title: string;
}

export type NotificationPermissionState = "granted" | "prompt" | "denied" | "unknown";

let suppressPermissionPrompt = false;

export function sessionNotificationName(liveTitle: string, listedTitle = "", preview = ""): string {
  const listed = listedTitle.trim();
  if (listed) return listed;
  const live = liveTitle.trim();
  if (live && live !== "新会话") return live;
  const text = preview.trim();
  if (text) return text.length > 200 ? text.slice(0, 200) : text;
  return "未命名会话";
}

export function sessionNotificationCopy(phase: SessionNotifyPhase, title: string): { title: string; body: string } {
  const name = title.trim() || "未命名会话";
  const body = name.length > 120 ? `${name.slice(0, 119)}…` : name;
  const label = phase === "start" ? "会话开始执行" : "会话执行完毕";
  return {
    title: `${label} · ${body}`,
    body,
  };
}

export function diffSessionNotifications(
  previous: ReadonlyMap<string, boolean> | null,
  current: ReadonlyMap<string, SessionNotifyState>,
): { next: Map<string, boolean>; intents: SessionNotifyIntent[] } {
  const next = new Map<string, boolean>();
  const intents: SessionNotifyIntent[] = [];
  for (const [sessionId, session] of current) {
    next.set(sessionId, session.busy);
    if (!previous) continue;
    const wasBusy = previous.get(sessionId) ?? false;
    if (wasBusy === session.busy) continue;
    intents.push({
      sessionId,
      phase: session.busy ? "start" : "complete",
      title: session.title,
    });
  }
  return { next, intents };
}

export async function readNotificationPermission(): Promise<NotificationPermissionState> {
  if (!isDesktopShell()) return "unknown";
  try {
    const { isPermissionGranted } = await import("@tauri-apps/plugin-notification");
    return await isPermissionGranted() ? "granted" : "prompt";
  } catch {
    return "unknown";
  }
}

export async function requestSessionNotificationPermission(): Promise<NotificationPermissionState> {
  if (!isDesktopShell()) return "unknown";
  suppressPermissionPrompt = false;
  try {
    const { isPermissionGranted, requestPermission } = await import("@tauri-apps/plugin-notification");
    if (await isPermissionGranted()) return "granted";
    return (await requestPermission()) === "granted" ? "granted" : "denied";
  } catch {
    return "unknown";
  }
}

async function notificationPermissionGranted(): Promise<boolean> {
  const { isPermissionGranted, requestPermission } = await import("@tauri-apps/plugin-notification");
  if (await isPermissionGranted()) {
    suppressPermissionPrompt = false;
    return true;
  }
  if (suppressPermissionPrompt) return false;
  const granted = (await requestPermission()) === "granted";
  suppressPermissionPrompt = !granted;
  return granted;
}

export async function showSessionNotification(phase: SessionNotifyPhase, title: string): Promise<void> {
  if (!isDesktopShell()) return;
  try {
    if (!await notificationPermissionGranted()) return;
    const { sendNotification } = await import("@tauri-apps/plugin-notification");
    sendNotification(sessionNotificationCopy(phase, title));
  } catch {
    // A missing notification permission or shell API should not interrupt the session.
  }
}
