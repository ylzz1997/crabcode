import type { SessionViewState } from "./types";

export interface SessionExecutionTask {
  label: string;
  startedAt: number;
}

export function sessionExecutionTask(
  sessions: Record<string, SessionViewState>,
  active: SessionViewState | null,
): SessionExecutionTask | null {
  const running = Object.values(sessions).filter((session) => session.busy);
  if (!running.length) return null;
  const focus = active?.busy
    ? active
    : running.reduce((latest, session) => (
      (session.runStartedAt ?? 0) >= (latest.runStartedAt ?? 0) ? session : latest
    ));
  const step = focus.currentStep?.label?.trim();
  const label = running.length > 1
    ? `${running.length} 个会话正在执行${step ? ` · ${step}` : ""}`
    : step ? `正在执行 · ${step}` : "正在执行任务";
  return { label, startedAt: focus.runStartedAt ?? 0 };
}
