/* @vitest-environment jsdom */
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { PermissionPicker } from "./App";
import type { PermissionPolicyInfo } from "./permissionPresentation";

it("shows inherited full access, actual plan restrictions, and clears details for an older session", async () => {
  (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const refresh = vi.fn();
  const change = vi.fn();
  const policy: PermissionPolicyInfo = {
    configured_mode: "bypassPermissions", effective_mode: "plan",
    allow: [], ask: [], deny: [{ tool: "Bash", command: "rm *" }], runtime_allow_count: 1,
  };
  const render = (next?: PermissionPolicyInfo) => act(() => root.render(
    <PermissionPicker value="default" disabled={false} policy={next} onRefresh={refresh} onChange={change} />,
  ));
  try {
    await render(policy);
    expect(container.textContent).toContain("默认 · 完全访问");
    await act(() => container.querySelector<HTMLButtonElement>("button")!.click());
    expect(refresh).toHaveBeenCalledOnce();
    expect(container.textContent).toContain("继承后：完全访问");
    expect(container.textContent).toContain("当前生效：计划模式（只读）");
    expect(container.textContent).toContain("禁止：Bash · 命令 rm *");
    expect(container.textContent).toContain("本会话另有 1 项");
    await render({ ...policy, effective_mode: "bypassPermissions" });
    expect(container.textContent).toContain("当前生效：完全访问");
    expect(container.querySelector(".permission-picker-trigger")?.classList.contains("danger")).toBe(true);
    await render();
    expect(container.textContent).toContain("默认 · 暂无法读取");
    expect(container.textContent).not.toContain("rm *");
    expect(container.textContent).not.toContain("继承后：完全访问");
    await act(() => container.querySelector<HTMLButtonElement>('[role="menuitemradio"]')!.click());
    expect(change).toHaveBeenCalledWith("default");
  } finally {
    await act(() => root.unmount());
    container.remove();
  }
});
