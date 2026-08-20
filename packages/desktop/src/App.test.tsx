/* @vitest-environment jsdom */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ScheduleDeleteModal } from "./App";
import type { ScheduleJobInfo } from "./types";

const job: ScheduleJobInfo = {
  id: "job-1",
  name: "依赖巡检",
  prompt: "检查依赖",
  schedule: "+1h",
  schedule_type: "once",
  cwd: "/work/crabcode",
  enabled: false,
  status: "error",
  last_run: null,
  next_run: null,
  run_count: 1,
  max_runs: null,
  created_at: "2026-08-20T00:00:00Z",
  session_id: null,
  description: "",
  tags: [],
  timeout: 600,
  model_profile: null,
  extra: {},
  running: false,
};

describe("ScheduleDeleteModal", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it("requires an explicit in-app confirmation before deleting", () => {
    const onClose = vi.fn();
    const onConfirm = vi.fn();
    act(() => root.render(
      <ScheduleDeleteModal
        job={job}
        busy={false}
        error={null}
        onClose={onClose}
        onConfirm={onConfirm}
      />,
    ));

    expect(container.querySelector('[role="dialog"]')?.textContent).toContain("删除“依赖巡检”？");
    const buttons = Array.from(container.querySelectorAll<HTMLButtonElement>(".modal-actions button"));
    act(() => buttons.find((button) => button.textContent?.includes("永久删除"))!.click());

    expect(onConfirm).toHaveBeenCalledOnce();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("disables both actions while deletion is in progress", () => {
    act(() => root.render(
      <ScheduleDeleteModal
        job={job}
        busy
        error={null}
        onClose={vi.fn()}
        onConfirm={vi.fn()}
      />,
    ));

    const buttons = Array.from(container.querySelectorAll<HTMLButtonElement>(".modal-actions button"));
    expect(buttons).toHaveLength(2);
    expect(buttons.every((button) => button.disabled)).toBe(true);
  });
});
