/* @vitest-environment jsdom */
import { afterEach, describe, expect, it, vi } from "vitest";
import { shellProgressUpdate } from "./taskbarProgress";

const setProgressBar = vi.fn(() => Promise.resolve());

vi.mock("@tauri-apps/api/window", () => ({
  getCurrentWindow: () => ({ setProgressBar }),
  ProgressBarStatus: {
    None: "none",
    Normal: "normal",
    Indeterminate: "indeterminate",
  },
}));

import { setSessionTaskbarProgress } from "./taskbarProgress";

function setPlatform(platform: string) {
  Object.defineProperty(navigator, "platform", { value: platform, configurable: true });
  Object.defineProperty(navigator, "userAgent", { value: platform, configurable: true });
}

describe("session icon progress", () => {
  afterEach(async () => {
    vi.useRealTimers();
    setPlatform("Win32");
    Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
    await setSessionTaskbarProgress(false);
    setProgressBar.mockClear();
    Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
  });

  it("uses an indeterminate bar under the Windows taskbar icon", async () => {
    setPlatform("Win32");
    Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
    await setSessionTaskbarProgress(true);
    expect(setProgressBar).toHaveBeenCalledWith({ status: "indeterminate" });
    await setSessionTaskbarProgress(false);
    expect(setProgressBar).toHaveBeenLastCalledWith({ status: "none" });
  });

  it("animates a bar under the Dock icon while a session is running", async () => {
    vi.useFakeTimers();
    setPlatform("MacIntel");
    Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
    const pending = setSessionTaskbarProgress(true);
    await vi.runAllTicks();
    await pending;
    expect(setProgressBar).toHaveBeenCalledWith({ status: "normal", progress: 15 });
    await vi.advanceTimersByTimeAsync(180);
    expect(setProgressBar).toHaveBeenLastCalledWith({ status: "normal", progress: 25 });
    await setSessionTaskbarProgress(false);
    expect(setProgressBar).toHaveBeenLastCalledWith({ status: "none" });
    const calls = setProgressBar.mock.calls.length;
    await vi.advanceTimersByTimeAsync(1_000);
    expect(setProgressBar).toHaveBeenCalledTimes(calls);
  });

  it("does nothing outside the desktop shell", async () => {
    setPlatform("Win32");
    await setSessionTaskbarProgress(true);
    expect(setProgressBar).not.toHaveBeenCalled();
  });

  it("keeps Windows indeterminate and gives macOS a moving percentage", () => {
    expect(shellProgressUpdate(true, "windows", 3)).toEqual({ status: "indeterminate" });
    expect(shellProgressUpdate(true, "mac", 0)).toEqual({ status: "normal", progress: 15 });
    expect(shellProgressUpdate(true, "mac", 8)).toEqual({ status: "normal", progress: 15 });
    expect(shellProgressUpdate(false, "mac", 2)).toEqual({ status: "none" });
  });
});
