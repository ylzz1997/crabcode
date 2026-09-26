import { isDesktopShell } from "./native";

export type ShellProgressPlatform = "windows" | "mac" | "other";

export interface ShellProgressUpdate {
  status: "none" | "indeterminate" | "normal";
  progress?: number;
}

let progressGeneration = 0;
let animationTimer: number | null = null;
let animationTick = 0;

export function shellProgressPlatform(
  platform = typeof navigator === "undefined" ? "" : navigator.platform,
  userAgent = typeof navigator === "undefined" ? "" : navigator.userAgent,
): ShellProgressPlatform {
  const value = `${platform} ${userAgent}`;
  if (/Mac|iPhone|iPad/i.test(value)) return "mac";
  if (/Win/i.test(value)) return "windows";
  return "other";
}

export function shellProgressUpdate(
  running: boolean,
  platform: ShellProgressPlatform,
  tick: number,
): ShellProgressUpdate {
  if (!running) return { status: "none" };
  if (platform === "mac") {
    // Dock draws one left-aligned bar, so a looping value reads as activity under the icon.
    return { status: "normal", progress: 15 + (tick % 8) * 10 };
  }
  return { status: "indeterminate" };
}

function stopDockAnimation() {
  if (animationTimer == null) return;
  window.clearInterval(animationTimer);
  animationTimer = null;
}

async function paintShellProgress(update: ShellProgressUpdate, generation: number): Promise<void> {
  const { getCurrentWindow, ProgressBarStatus } = await import("@tauri-apps/api/window");
  if (generation !== progressGeneration) return;
  const status = update.status === "none"
    ? ProgressBarStatus.None
    : update.status === "normal"
      ? ProgressBarStatus.Normal
      : ProgressBarStatus.Indeterminate;
  await getCurrentWindow().setProgressBar(
    update.progress == null ? { status } : { status, progress: update.progress },
  );
}

export async function setSessionTaskbarProgress(running: boolean): Promise<void> {
  if (!isDesktopShell()) return;
  stopDockAnimation();
  const generation = ++progressGeneration;
  const platform = shellProgressPlatform();
  try {
    if (running && platform === "mac") {
      animationTick = 0;
      await paintShellProgress(shellProgressUpdate(true, platform, animationTick), generation);
      if (generation !== progressGeneration) return;
      animationTimer = window.setInterval(() => {
        animationTick += 1;
        void paintShellProgress(shellProgressUpdate(true, platform, animationTick), generation).catch(() => undefined);
      }, 180);
      return;
    }
    await paintShellProgress(shellProgressUpdate(running, platform, 0), generation);
  } catch {
    // Icon progress is only a hint. A missing shell API should not interrupt the session.
  }
}
