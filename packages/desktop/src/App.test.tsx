/* @vitest-environment jsdom */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  collectFavoriteSessions,
  FavoritesView,
  formatTurnDuration,
  resolveRememberedModel,
  ScheduleDeleteModal,
} from "./App";
import type { ConnectionPreset, GatewayViewState, ScheduleJobInfo } from "./types";

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

describe("turn duration formatting", () => {
  it("supports seconds and omits zero hours/minutes in hms mode", () => {
    expect(formatTurnDuration(3_723_000, "seconds")).toBe("3723秒");
    expect(formatTurnDuration(3_723_000, "hms")).toBe("1时2分3秒");
    expect(formatTurnDuration(63_000, "hms")).toBe("1分3秒");
    expect(formatTurnDuration(3_000, "hms")).toBe("3秒");
  });
});

describe("favorite sessions", () => {
  const project = {
    id: "project-1",
    path: "/work/crab",
    name: "CrabCode",
    directories: ["/work/crab"],
    last_session_id: null,
    favorite_session_ids: ["session-1"],
  };
  const connection = {
    id: "local",
    name: "Local",
    base_url: "http://127.0.0.1:4096",
    credential_ref: null,
    allow_insecure_remote: false,
    projects: [project],
    last_project_path: project.path,
    last_project_id: project.id,
  } as ConnectionPreset;
  const gateway = {
    status: "online",
    error: null,
    token: null,
    tokenExpiresAt: 0,
    workspace: null,
    models: [],
    sessionsByProject: {
      [project.path]: [{
        session_id: "session-1",
        message_count: 2,
        model: "",
        provider: "",
        created_at: "2026-08-20T00:00:00Z",
        title: "重要会话",
        cwd: project.path,
        tokens_used: 0,
        preview: "检查发布状态",
      }],
    },
    runningCount: 0,
    pendingCount: 0,
  } as GatewayViewState;

  it("collects favorite sessions with their project", () => {
    expect(collectFavoriteSessions(connection, gateway)).toMatchObject([{
      project: { name: "CrabCode" },
      session: { title: "重要会话" },
    }]);
  });

  it("shows the project name and opens a favorite session", () => {
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const item = collectFavoriteSessions(connection, gateway)[0];
    const onOpen = vi.fn();
    act(() => root.render(<FavoritesView items={[item]} connected onOpen={onOpen} onToggle={vi.fn()} />));
    expect(container.textContent).toContain("CrabCode");
    act(() => container.querySelector<HTMLButtonElement>(".favorite-session-main")!.click());
    expect(onOpen).toHaveBeenCalledWith(item);
    act(() => root.unmount());
    container.remove();
  });
});

describe("remembered model selection", () => {
  it("uses the remembered profile while it is still advertised by the Gateway", () => {
    expect(resolveRememberedModel(
      { last_model_profile: "fast" },
      [{ name: "fast" }, { name: "smart" }],
    )).toBe("fast");
  });

  it("omits a removed profile so the Gateway default is used", () => {
    expect(resolveRememberedModel(
      { last_model_profile: "removed" },
      [{ name: "fast" }, { name: "smart" }],
    )).toBeUndefined();
  });
});
