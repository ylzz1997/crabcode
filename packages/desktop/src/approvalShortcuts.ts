import { t as tr } from "./i18n";
import type { ApprovalShortcutPreferences, ChatItem, SessionViewState } from "./types";

export const APPROVE_SHORTCUT = "Ctrl+Alt+Shift+F9";
export const DENY_SHORTCUT = "Ctrl+Alt+Shift+F10";
export const ALWAYS_ALLOW_SHORTCUT = "Ctrl+Alt+Shift+F11";
export const DEFAULT_APPROVAL_SHORTCUTS: ApprovalShortcutPreferences = {
  enabled: true,
  approve: APPROVE_SHORTCUT,
  deny: DENY_SHORTCUT,
  always_allow: ALWAYS_ALLOW_SHORTCUT,
};

export function normalizeApprovalShortcut(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const parts = value.toUpperCase().split("+").map((part) => part.trim());
  const key = parts.pop();
  if (!key || !/^(?:[A-Z0-9]|F(?:[1-9]|1\d|2[0-4]))$/.test(key)) return null;
  if (!parts.every((part) => ["CTRL", "ALT", "SHIFT", "SUPER"].includes(part))) return null;
  if (new Set(parts).size !== parts.length || !parts.some((part) => ["CTRL", "ALT", "SUPER"].includes(part))) return null;
  return [
    ...(parts.includes("CTRL") ? ["Ctrl"] : []),
    ...(parts.includes("ALT") ? ["Alt"] : []),
    ...(parts.includes("SHIFT") ? ["Shift"] : []),
    ...(parts.includes("SUPER") ? ["Super"] : []),
    key,
  ].join("+");
}

export function normalizeApprovalShortcuts(value: unknown): ApprovalShortcutPreferences {
  const raw = value && typeof value === "object" ? value as Record<string, unknown> : {};
  const approve = normalizeApprovalShortcut(raw.approve) ?? APPROVE_SHORTCUT;
  const deny = normalizeApprovalShortcut(raw.deny) ?? DENY_SHORTCUT;
  const alwaysAllow = normalizeApprovalShortcut(raw.always_allow) ?? ALWAYS_ALLOW_SHORTCUT;
  if (new Set([approve, deny, alwaysAllow]).size !== 3) {
    return { ...DEFAULT_APPROVAL_SHORTCUTS, enabled: raw.enabled !== false };
  }
  return {
    enabled: raw.enabled !== false,
    approve,
    deny,
    always_allow: alwaysAllow,
  };
}

export function approvalShortcutFromKey(event: Pick<KeyboardEvent, "code" | "ctrlKey" | "altKey" | "shiftKey" | "metaKey">): string | null {
  const key = event.code.replace(/^Key|^Digit/, "");
  return normalizeApprovalShortcut([
    ...(event.ctrlKey ? ["Ctrl"] : []),
    ...(event.altKey ? ["Alt"] : []),
    ...(event.shiftKey ? ["Shift"] : []),
    ...(event.metaKey ? ["Super"] : []),
    key,
  ].join("+"));
}

export interface PendingApproval {
  sessionKey: string;
  item: ChatItem;
}

export function uniquePendingApproval(sessions: Record<string, SessionViewState>): PendingApproval | null {
  const pending = Object.entries(sessions).flatMap(([sessionKey, session]) =>
    session.connected
      ? session.items.filter((item) => item.kind === "permission" && item.status === "pending")
        .map((item) => ({ sessionKey, item }))
      : [],
  );
  return pending.length === 1 && pending[0].item.tool_use_id ? pending[0] : null;
}

export function approvalShortcutKeyLabels(
  shortcut: string,
  platform = typeof navigator === "undefined" ? "" : navigator.platform || navigator.userAgent,
): string[] {
  const mac = /Macintosh|MacIntel|MacPPC|Mac68K/i.test(platform);
  const labels: Record<string, string> = mac
    ? { Ctrl: "⌃", Alt: "⌥", Shift: "⇧", Super: "⌘" }
    : { Super: /Win/i.test(platform) ? "Win" : "Super" };
  return shortcut.split("+").map((key) => labels[key] ?? key);
}

// Serialize reconfiguration so cleanup cannot unregister a newer binding.
let registrationQueue: Promise<void> = Promise.resolve();

export function bindApprovalShortcuts(
  preferences: ApprovalShortcutPreferences,
  onDecision: (allowed: boolean, alwaysAllow: boolean) => void,
  onError: (message: string | null) => void,
): { ready: Promise<void>; dispose: () => Promise<void> } {
  let disposed = false;
  let armed = false;
  let native: typeof import("@tauri-apps/plugin-global-shortcut") | null = null;
  const registered: string[] = [];
  const bindings = [
    { shortcut: preferences.approve, allowed: true, alwaysAllow: false },
    { shortcut: preferences.deny, allowed: false, alwaysAllow: false },
    { shortcut: preferences.always_allow, allowed: true, alwaysAllow: true },
  ];
  const unregister = async () => {
    if (!native) return;
    await Promise.allSettled(registered.splice(0).map((shortcut) => native!.unregister(shortcut)));
  };
  const register = async () => {
    if (disposed || !preferences.enabled) return;
    try {
      native = await import("@tauri-apps/plugin-global-shortcut");
      for (const binding of bindings) {
        if (disposed) return;
        await native.register(binding.shortcut, (event) => {
          if (!disposed && armed && event.state === "Pressed") onDecision(binding.allowed, binding.alwaysAllow);
        });
        registered.push(binding.shortcut);
      }
      if (!disposed) { armed = true; onError(null); }
    } catch (error) {
      if (!disposed) onError(tr("权限快捷键注册失败，可在设置 → 常规 → 权限快捷键中更换按键：{0}", { 0: error instanceof Error ? error.message : String(error) }));
    } finally {
      if (disposed || registered.length !== bindings.length) await unregister();
    }
  };
  const ready = registrationQueue = registrationQueue.then(register, register);
  return {
    ready,
    dispose: () => {
      disposed = true;
      armed = false;
      return registrationQueue = registrationQueue.then(unregister, unregister);
    },
  };
}
