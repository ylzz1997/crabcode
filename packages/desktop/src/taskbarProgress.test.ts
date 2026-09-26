/* @vitest-environment jsdom */
import { afterEach, describe, expect, it, vi } from "vitest";

const setProgressBar = vi.fn(() => Promise.resolve());

vi.mock("@tauri-apps/api/window", () => ({
  getCurrentWindow: () => ({ setProgressBar }),
  ProgressBarStatus: {
    None: "none",
    Indeterminate: "indeterminate",
  },
}));

import { setSessionTaskbarProgress } from "./taskbarProgress";

describe("session taskbar progress", () => {
  afterEach(() => {
    setProgressBar.mockClear();
    Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
  });

  it("shows an indeterminate taskbar progress while a session is running", async () => {
    Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
    await setSessionTaskbarProgress(true);
    expect(setProgressBar).toHaveBeenCalledWith({ status: "indeterminate" });
    await setSessionTaskbarProgress(false);
    expect(setProgressBar).toHaveBeenLastCalledWith({ status: "none" });
  });

  it("does nothing outside the desktop shell", async () => {
    await setSessionTaskbarProgress(true);
    expect(setProgressBar).not.toHaveBeenCalled();
  });
});
