/* @vitest-environment jsdom */
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { ModelSettingsPanel } from "./ModelSettingsPanel";
import type { GatewayViewState, ModelSettingsResponse, ConnectionPreset } from "./types";

const data: ModelSettingsResponse = {
  cwd: "/project", sources: [], default_model: null, warnings: [],
  groups: { shared: { provider: "openai" } },
  models: [{ name: "custom alias", group: "shared", is_default: false, configured: { model: "old" }, effective: { model: "old" }, overridden_fields: [], sources: [] }],
  editable_sources: [{ id: "userSettings", label: "用户配置", path: "/user/settings.json", exists: true, writable: true }],
};

it("previews discovered settings and imports metadata only for selected new models", async () => {
  (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const config = { context_window: 131072, max_tokens: 8192, thinking_enabled: false };
  const onDiscover = vi.fn(async () => ({ group: "shared", models: [{ id: "old", config }, { id: "org/new", config }, { id: "other", config }] }));
  const onMutate = vi.fn().mockRejectedValueOnce(new Error("write failed")).mockResolvedValue(undefined);
  const props = { activeConnection: null, activeProject: null, gateway: { status: "online" } as GatewayViewState, data, loading: false, error: null, onRefresh: () => {}, onMutate, onDiscover };
  const click = async (text: string) => act(async () => [...container.querySelectorAll<HTMLButtonElement>("button")].find(button => button.textContent === text)!.click());
  try {
    await act(async () => root.render(<ModelSettingsPanel {...props} />));
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="shared 更多操作"]')!.click());
    await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="获取 shared 的模型列表"]')!.click());
    expect(onDiscover).toHaveBeenCalledWith("shared");
    expect(container.querySelector<HTMLInputElement>('[aria-label="old"]')!.disabled).toBe(true);
    expect(onMutate).not.toHaveBeenCalled();
    expect(container.textContent).toContain("上下文窗口: 131072");
    expect(container.textContent).toContain("最大输出 Token: 8192");
    expect(container.textContent).toContain("思考: 关闭");
    await act(async () => container.querySelector<HTMLInputElement>('[aria-label="org/new"]')!.click());
    await click("添加所选模型");
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("write failed");
    expect(container.querySelector<HTMLInputElement>('[aria-label="org/new"]')!.checked).toBe(true);
    await click("添加所选模型");
    expect(onMutate).toHaveBeenLastCalledWith({ action: "import_group_models", name: "shared", source: "userSettings", cwd: undefined, model_ids: ["org/new"], model_metadata: { "org/new": config } });
    expect(container.querySelector('[role="dialog"]')).toBeNull();
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});

it("closes discovery and discards pending results when the connection changes", async () => {
  (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  let finish!: (value: { group: string; models: { id: string }[] }) => void;
  const onDiscover = vi.fn(() => new Promise<{ group: string; models: { id: string }[] }>(resolve => { finish = resolve; }));
  const props = { activeConnection: null, activeProject: null, gateway: { status: "online" } as GatewayViewState, data: { ...data, models: [] }, loading: false, error: null, onRefresh: () => {}, onMutate: vi.fn(async () => {}), onDiscover };
  try {
    await act(async () => root.render(<ModelSettingsPanel {...props} />));
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="shared 更多操作"]')!.click());
    await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="获取 shared 的模型列表"]')!.click());
    expect(container.textContent).toContain("正在获取模型列表");
    await act(async () => root.render(<ModelSettingsPanel {...props} activeConnection={{ id: "other" } as ConnectionPreset} />));
    await act(async () => finish({ group: "shared", models: [{ id: "stale" }] }));
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(container.textContent).not.toContain("stale");
    expect(props.onMutate).not.toHaveBeenCalled();
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});
