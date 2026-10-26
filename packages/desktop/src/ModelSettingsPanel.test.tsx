/* @vitest-environment jsdom */
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { ModelSettingsPanel } from "./ModelSettingsPanel";
import type { GatewayViewState, ModelSettingsResponse } from "./types";

function deleteTestData(): ModelSettingsResponse {
  return {
    cwd: "/project", default_model: null, groups: {}, sources: ["/home/.crabcode/settings.json"], warnings: [],
    editable_sources: [{ id: "userSettings", label: "用户配置", path: "/home/.crabcode/settings.json", exists: true, writable: true }],
    models: [{ name: "example", group: null, is_default: false, configured: { model: "example" }, effective: { model: "example" }, overridden_fields: ["model"], sources: ["/home/.crabcode/settings.json"] }],
  };
}

it("opens an in-app delete confirmation and removes the model from its source", async () => {
  (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const onMutate = vi.fn(async () => {});
  const nativeConfirm = vi.spyOn(window, "confirm");
  try {
    await act(async () => root.render(<ModelSettingsPanel activeConnection={null} activeProject={null} gateway={{ status: "online" } as GatewayViewState} data={deleteTestData()} loading={false} error={null} onRefresh={() => {}} onMutate={onMutate} />));
    const deleteButton = () => container.querySelector<HTMLButtonElement>('[aria-label="删除模型 example"]')!;
    await act(async () => deleteButton().click());
    expect(container.querySelector('[role="dialog"][aria-label="删除模型"]')?.textContent).toContain("用户配置");
    expect(onMutate).not.toHaveBeenCalled();
    await act(async () => container.querySelector<HTMLButtonElement>('.model-delete-dialog button')!.click());
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    await act(async () => deleteButton().click());
    await act(async () => Array.from(container.querySelectorAll<HTMLButtonElement>('.model-delete-dialog button')).find(button => button.textContent?.includes("确认删除"))!.click());
    expect(onMutate).toHaveBeenCalledWith({ action: "delete_model", source: "userSettings", cwd: undefined, name: "example" });
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(nativeConfirm).not.toHaveBeenCalled();
  } finally {
    nativeConfirm.mockRestore();
    await act(async () => root.unmount());
    container.remove();
  }
});

it("keeps the delete dialog open and shows a failed mutation", async () => {
  (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const onMutate = vi.fn(async () => { throw new Error("模型不在所选配置层中"); });
  try {
    await act(async () => root.render(<ModelSettingsPanel activeConnection={null} activeProject={null} gateway={{ status: "online" } as GatewayViewState} data={deleteTestData()} loading={false} error={null} onRefresh={() => {}} onMutate={onMutate} />));
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="删除模型 example"]')!.click());
    await act(async () => Array.from(container.querySelectorAll<HTMLButtonElement>('.model-delete-dialog button')).find(button => button.textContent?.includes("确认删除"))!.click());
    expect(container.querySelector('[role="dialog"][aria-label="删除模型"]')).not.toBeNull();
    expect(container.querySelector('.model-delete-dialog [role="alert"]')?.textContent).toContain("模型不在所选配置层中");
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});

it("deletes a group from the layer that defines it", async () => {
  (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const onMutate = vi.fn(async () => {});
  const data = deleteTestData();
  data.groups = { shared: { provider: "openai" } };
  data.group_sources = { shared: ["/project/.crabcode/settings.json"] };
  data.editable_sources!.push({ id: "projectSettings", label: "项目配置", path: "/project/.crabcode/settings.json", exists: true, writable: true });
  try {
    await act(async () => root.render(<ModelSettingsPanel activeConnection={null} activeProject={null} gateway={{ status: "online" } as GatewayViewState} data={data} loading={false} error={null} onRefresh={() => {}} onMutate={onMutate} />));
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="shared 更多操作"]')!.click());
    await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="删除配置组 shared"]')!.click());
    expect(container.querySelector('[role="dialog"][aria-label="删除配置组"]')?.textContent).toContain("项目配置");
    await act(async () => Array.from(container.querySelectorAll<HTMLButtonElement>('.model-delete-dialog button')).find(button => button.textContent?.includes("确认删除"))!.click());
    expect(onMutate).toHaveBeenCalledWith({ action: "delete_group", source: "projectSettings", cwd: undefined, name: "shared" });
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});

it("tests the selected model, blocks duplicate clicks and discards stale results", async () => {
  (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const data: ModelSettingsResponse = {
    cwd: "D:/project", default_model: "A", groups: {}, sources: [], warnings: [],
    models: ["A", "B"].map(name => ({ name, group: null, is_default: name === "A", configured: {}, effective: {}, overridden_fields: [], sources: [] })),
  };
  let finish!: (result: { ok: boolean; message: string }) => void;
  const onTest = vi.fn(() => new Promise<{ ok: boolean; message: string }>(resolve => { finish = resolve; }));
  const button = () => Array.from(container.querySelectorAll("button")).find(b => /测试模型|测试中/.test(b.textContent || ""))!;
  try {
    await act(async () => root.render(<ModelSettingsPanel activeConnection={null} activeProject={null} gateway={{ status: "online" } as GatewayViewState} data={data} loading={false} error={null} onRefresh={() => {}} onTest={onTest} />));
    await act(async () => button().click());
    expect(onTest).toHaveBeenCalledWith("A");
    expect(button().disabled).toBe(true);
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="查看模型 B"]')!.click());
    await act(async () => finish({ ok: true, message: "old A result" }));
    expect(container.textContent).not.toContain("old A result");
    await act(async () => button().click());
    expect(onTest).toHaveBeenLastCalledWith("B");
    await act(async () => finish({ ok: false, message: "HTTP 401：认证失败" }));
    expect(container.querySelector('[role="status"]')?.textContent).toContain("HTTP 401");
    expect(container.querySelector('[role="status"]')?.classList.contains("model-test-success")).toBe(false);
    await act(async () => button().click());
    await act(async () => finish({ ok: true, message: "连接成功" }));
    expect(container.querySelector('[role="status"]')?.textContent).toContain("连接成功");
    expect(container.querySelector('[role="status"]')?.classList.contains("model-test-success")).toBe(true);
    expect(container.textContent).not.toContain("HTTP 401");
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});
