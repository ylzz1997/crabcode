import type { Memento } from "vscode";
import type { SessionRuntimeStatus } from "./client/types";

export const REASONING_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"] as const;
export type ReasoningEffort = typeof REASONING_EFFORTS[number];
type Preferences = { reasoning_effort?: ReasoningEffort | "auto"; ultra_mode?: boolean };
export type RuntimeControlsState = Preferences & { ready: boolean; pending: boolean };

export function normalizeRuntimePreferences(value: unknown): Preferences {
  const result: Preferences = {};
  if (!value || typeof value !== "object") return result;
  const source = value as Record<string, unknown>;
  if (REASONING_EFFORTS.includes(source.reasoning_effort as ReasoningEffort)) {
    result.reasoning_effort = source.reasoning_effort as ReasoningEffort;
  }
  if (typeof source.ultra_mode === "boolean") result.ultra_mode = source.ultra_mode;
  return result;
}

/** Serialize reads, restoration and changes per gateway/session so late replies
 * cannot overwrite a newer selection or leak into a different conversation. */
export class RuntimeControls {
  private states = new Map<string, RuntimeControlsState>();
  private queues = new Map<string, Promise<void>>();

  constructor(
    private readonly storage: Memento | undefined,
    private readonly request: (path: string, sessionId: string, body?: Preferences) => Promise<SessionRuntimeStatus>,
    private readonly publish: (sessionId: string, state: RuntimeControlsState) => void,
    private readonly reportError: (sessionId: string, message: string) => void,
    private readonly gatewayKey: () => string,
  ) {}

  private key(sessionId: string): string {
    return `crabcode.runtimeControls:${JSON.stringify([this.gatewayKey(), sessionId])}`;
  }

  get(sessionId: string): RuntimeControlsState {
    return this.states.get(this.key(sessionId)) ?? { ready: false, pending: false };
  }

  private run(sessionId: string, action: (key: string) => Promise<void>): Promise<void> {
    const key = this.key(sessionId);
    const previous = this.queues.get(key) ?? Promise.resolve();
    const operation = previous.then(async () => {
      try {
        await action(key);
      } catch (error) {
        this.reportError(sessionId, `会话设置失败：${error instanceof Error ? error.message : String(error)}`);
      }
    });
    this.queues.set(key, operation);
    this.states.set(key, { ...this.get(sessionId), pending: true });
    this.publish(sessionId, this.get(sessionId));
    void operation.then(() => {
      if (this.queues.get(key) !== operation) return;
      this.queues.delete(key);
      const state = { ...this.states.get(key)!, pending: false };
      this.states.set(key, state);
      if (this.key(sessionId) === key) this.publish(sessionId, state);
    });
    return operation;
  }

  whenSettled(sessionId: string): Promise<void> {
    return this.queues.get(this.key(sessionId)) ?? Promise.resolve();
  }

  refresh(sessionId: string, restore = false): Promise<void> {
    return this.run(sessionId, async (key) => {
      if (key !== this.key(sessionId)) return;
      const status = await this.request("/session/status", sessionId);
      this.states.set(key, { ...normalizeRuntimePreferences(status), ready: true, pending: true });
      if (!restore) return;
      const saved = normalizeRuntimePreferences(this.storage?.get(key));
      if (saved.reasoning_effort && saved.reasoning_effort !== status.reasoning_effort) {
        await this.apply(sessionId, key, { reasoning_effort: saved.reasoning_effort });
      }
      if (typeof saved.ultra_mode === "boolean" && saved.ultra_mode !== status.ultra_mode) {
        await this.apply(sessionId, key, { ultra_mode: saved.ultra_mode });
      }
    });
  }

  setEffort(sessionId: string, effort: string): Promise<void> {
    if (effort === "auto") {
      return this.run(sessionId, (key) => this.clearEffort(sessionId, key));
    }
    if (!REASONING_EFFORTS.includes(effort as ReasoningEffort)) {
      this.reportError(sessionId, "无效的思考强度。");
      return Promise.resolve();
    }
    return this.run(sessionId, (key) => this.apply(sessionId, key, { reasoning_effort: effort as ReasoningEffort }));
  }

  setUltra(sessionId: string, enabled: boolean | null): Promise<void> {
    return this.run(sessionId, async (key) => {
      if (key !== this.key(sessionId)) return;
      // Toggle against the server's current value, including slash commands.
      const status = enabled === null ? await this.request("/session/status", sessionId) : null;
      await this.apply(sessionId, key, { ultra_mode: enabled ?? !status?.ultra_mode });
    });
  }

  private async clearEffort(sessionId: string, key: string): Promise<void> {
    if (key !== this.key(sessionId)) return;
    if (!this.states.get(key)?.ready) {
      const status = await this.request("/session/status", sessionId);
      this.states.set(key, { ...normalizeRuntimePreferences(status), ready: true, pending: true });
    }
    if (key !== this.key(sessionId)) return;
    const normalized = normalizeRuntimePreferences(
      await this.request("/config/reasoning-effort", sessionId, { reasoning_effort: "auto" }),
    );
    const next: RuntimeControlsState = {
      ...this.states.get(key),
      ...normalized,
      ready: true,
      pending: true,
    };
    if (normalized.reasoning_effort) next.reasoning_effort = normalized.reasoning_effort;
    else delete next.reasoning_effort;
    this.states.set(key, next);
    const stored: Preferences = {
      ...normalizeRuntimePreferences(this.storage?.get(key)),
      ...normalized,
    };
    if (normalized.reasoning_effort) stored.reasoning_effort = normalized.reasoning_effort;
    else delete stored.reasoning_effort;
    await this.storage?.update(key, Object.keys(stored).length > 0 ? stored : undefined);
  }

  private async apply(sessionId: string, key: string, preference: Preferences): Promise<void> {
    if (key !== this.key(sessionId)) return;
    if (!this.states.get(key)?.ready) {
      const status = await this.request("/session/status", sessionId);
      this.states.set(key, { ...normalizeRuntimePreferences(status), ready: true, pending: true });
    }
    if (key !== this.key(sessionId)) return;
    const endpoint = preference.reasoning_effort ? "/config/reasoning-effort" : "/config/ultra-mode";
    const result = normalizeRuntimePreferences(await this.request(endpoint, sessionId, preference));
    const field = preference.reasoning_effort ? "reasoning_effort" : "ultra_mode";
    if (result[field] === undefined) throw new Error("网关未确认设置，请重试。");
    this.states.set(key, { ...this.states.get(key), ...result, ready: true, pending: true });
    // Only persist settings that the gateway actually accepted.
    await this.storage?.update(key, { ...normalizeRuntimePreferences(this.storage.get(key)), ...result });
  }
}
