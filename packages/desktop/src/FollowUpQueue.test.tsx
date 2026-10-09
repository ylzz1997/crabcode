/* @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { FollowUpQueue } from "./FollowUpQueue";
import type { PendingFollowUp } from "./types";

let root: Root;
let container: HTMLDivElement;
const onAction = vi.fn();
const onDisableQueue = vi.fn();
const entry: PendingFollowUp = { item: { id: "q1", kind: "user", text: "你好" }, text: "你好", images: [], status: "pending" };

beforeEach(() => {
  (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  onAction.mockClear();
  onDisableQueue.mockClear();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); });
function render(messages = [entry], canSteer = true, queueEnabled = true) {
  act(() => root.render(<FollowUpQueue messages={messages} canSteer={canSteer} queueEnabled={queueEnabled} onAction={onAction} onDisableQueue={onDisableQueue} />));
}
function click(label: string) {
  const button = document.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)
    ?? [...document.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent === label)!;
  act(() => button.click());
  return button;
}

it("offers steer/delete on the card and edit/disable queue only in its menu", () => {
  render();
  expect(container.textContent).toBe("你好引导");
  expect(document.querySelector('[role="menu"]')).toBeNull();
  click("引导当前运行");
  click("删除排队消息");
  expect(onAction.mock.calls).toEqual([["q1", "steer"], ["q1", "remove"]]);
  click("更多排队操作");
  expect([...document.querySelectorAll('[role="menuitem"]')].map((item) => item.textContent)).toEqual(["编辑消息", "关闭排队"]);
  click("编辑消息");
  expect(onAction).toHaveBeenLastCalledWith("q1", "edit");
  expect(document.querySelector('[role="menu"]')).toBeNull();
  click("更多排队操作");
  click("关闭排队");
  expect(onDisableQueue).toHaveBeenCalledOnce();
  expect(container.textContent).toBe("你好引导");
});

it("supports keyboard navigation and dismisses the menu with Escape or outside click", () => {
  render();
  const more = click("更多排队操作");
  expect(document.activeElement?.textContent).toBe("编辑消息");
  act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true })));
  expect(document.activeElement?.textContent).toBe("关闭排队");
  act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
  expect(document.querySelector('[role="menu"]')).toBeNull();
  expect(document.activeElement).toBe(more);
  click("更多排队操作");
  act(() => document.body.dispatchEvent(new Event("pointerdown", { bubbles: true })));
  expect(document.querySelector('[role="menu"]')).toBeNull();
});

it("prevents repeated actions while awaiting acknowledgement and lets unsent messages be edited", () => {
  render([{ ...entry, action: "edit" }]);
  click("引导当前运行");
  click("删除排队消息");
  click("更多排队操作");
  expect(onAction).not.toHaveBeenCalled();
  expect(document.querySelector('[role="menu"]')).toBeNull();
  render([{ ...entry, status: "cancelled" }], false, false);
  click("引导当前运行");
  expect(onAction).not.toHaveBeenCalled();
  click("更多排队操作");
  click("编辑消息");
  expect(onAction).toHaveBeenCalledWith("q1", "edit");
  render([]);
  expect(container.children).toHaveLength(0);
});
