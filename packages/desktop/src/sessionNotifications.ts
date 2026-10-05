import { isDesktopShell } from "./native";

export type SessionNotifyPhase = "start" | "complete" | "interaction";

export type SessionInteractionKind = "permission" | "choice" | "plan";

export interface SessionInteraction {
  id: string;
  kind: SessionInteractionKind;
  detail: string;
}

export interface SessionInteractionInput {
  id: string;
  kind: string;
  status?: string;
  title?: string;
  text?: string;
  tool_use_id?: string;
}

export interface SessionNotifyState {
  title: string;
  busy: boolean;
  interactions?: readonly SessionInteraction[];
}

export interface SessionNotifySnapshot {
  busy: boolean;
  interactionIds: readonly string[];
}

export interface SessionNotifyIntent {
  sessionId: string;
  phase: SessionNotifyPhase;
  title: string;
  detail?: string;
  interaction?: SessionInteractionKind;
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

const INTERACTION_LABEL: Record<SessionInteractionKind, string> = {
  permission: "需要确认权限",
  choice: "需要你的选择",
  plan: "需要确认计划",
};

function clipNotificationText(value: string): string {
  return value.length > 120 ? `${value.slice(0, 119)}…` : value;
}

export function sessionInteractions(items: readonly SessionInteractionInput[]): SessionInteraction[] {
  const seen = new Set<string>();
  const interactions: SessionInteraction[] = [];
  for (const item of items) {
    if (item.status !== "pending") continue;
    if (item.kind !== "permission" && item.kind !== "choice" && item.kind !== "plan") continue;
    const rawId = item.tool_use_id?.trim() || item.id.trim();
    if (!rawId) continue;
    const id = `${item.kind}:${rawId}`;
    if (seen.has(id)) continue;
    seen.add(id);
    interactions.push({
      id,
      kind: item.kind,
      detail: item.title?.trim() || item.text?.trim() || "",
    });
  }
  return interactions;
}

export function sessionNotificationCopy(
  phase: SessionNotifyPhase,
  title: string,
  detail = "",
  interaction?: SessionInteractionKind,
): { title: string; body: string } {
  const session = clipNotificationText(title.trim() || "未命名会话");
  if (phase === "interaction") {
    const prompt = detail.trim();
    return {
      title: `${interaction ? INTERACTION_LABEL[interaction] : "需要你的操作"} · ${session}`,
      body: prompt ? clipNotificationText(prompt) : session,
    };
  }
  const label = phase === "start" ? "会话开始执行" : "会话执行完毕";
  return {
    title: `${label} · ${session}`,
    body: session,
  };
}

export function diffSessionNotifications(
  previous: ReadonlyMap<string, SessionNotifySnapshot> | null,
  current: ReadonlyMap<string, SessionNotifyState>,
): { next: Map<string, SessionNotifySnapshot>; intents: SessionNotifyIntent[] } {
  const next = new Map<string, SessionNotifySnapshot>();
  const intents: SessionNotifyIntent[] = [];
  for (const [sessionId, session] of current) {
    const interactions = session.interactions ?? [];
    next.set(sessionId, {
      busy: session.busy,
      interactionIds: interactions.map((item) => item.id),
    });
    if (!previous) continue;
    const prior = previous.get(sessionId);
    if ((prior?.busy ?? false) !== session.busy) {
      intents.push({
        sessionId,
        phase: session.busy ? "start" : "complete",
        title: session.title,
      });
    }
    const known = new Set(prior?.interactionIds ?? []);
    for (const interaction of interactions) {
      if (known.has(interaction.id)) continue;
      intents.push({
        sessionId,
        phase: "interaction",
        title: session.title,
        detail: interaction.detail,
        interaction: interaction.kind,
      });
    }
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

export async function showSessionNotification(
  phase: SessionNotifyPhase,
  title: string,
  detail = "",
  interaction?: SessionInteractionKind,
): Promise<void> {
  if (!isDesktopShell()) return;
  try {
    if (!await notificationPermissionGranted()) return;
    const { sendNotification } = await import("@tauri-apps/plugin-notification");
    sendNotification(sessionNotificationCopy(phase, title, detail, interaction));
  } catch {
    // A missing notification permission or shell API should not interrupt the session.
  }
}
