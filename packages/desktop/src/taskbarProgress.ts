import { isDesktopShell } from "./native";

let progressGeneration = 0;

export async function setSessionTaskbarProgress(running: boolean): Promise<void> {
  if (!isDesktopShell()) return;
  const generation = ++progressGeneration;
  try {
    const { getCurrentWindow, ProgressBarStatus } = await import("@tauri-apps/api/window");
    if (generation !== progressGeneration) return;
    await getCurrentWindow().setProgressBar({
      status: running ? ProgressBarStatus.Indeterminate : ProgressBarStatus.None,
    });
  } catch {
    // Taskbar progress is only a hint. A missing shell API should not interrupt the session.
  }
}
