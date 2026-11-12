/* @vitest-environment jsdom */

import { afterEach, expect, it, vi } from "vitest";
import { bindChatScroll } from "../../shared/chatScroll.js";

afterEach(() => vi.unstubAllGlobals());

it("follows layout changes until the reader scrolls up and resumes at the bottom", () => {
  const viewport = document.createElement("div");
  const content = document.createElement("div");
  let height = 500;
  let top = 0;
  Object.defineProperties(viewport, {
    clientHeight: { value: 100 },
    scrollHeight: { get: () => height },
    scrollTop: {
      get: () => top,
      set: (value: number) => { top = Math.min(value, height - 100); },
    },
  });
  let resize = () => {};
  const disconnect = vi.fn();
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: () => void) { resize = callback; }
    observe() {}
    disconnect = disconnect;
  });
  const scroll = bindChatScroll(viewport, content);
  const moveTo = (position: number) => {
    viewport.scrollTop = position;
    viewport.dispatchEvent(new Event("scroll"));
  };
  const grow = () => {
    height += 50;
    resize();
  };

  expect(top).toBe(400);
  // A queued event from a previous scroll can arrive after the content grows.
  height += 50;
  moveTo(400);
  expect(scroll.isFollowing()).toBe(true);
  resize();
  expect(top).toBe(450);

  moveTo(440);
  expect(scroll.isFollowing()).toBe(false);
  grow();
  expect(top).toBe(440);
  scroll.jumpToBottom();
  expect(scroll.isFollowing()).toBe(true);
  grow();
  expect(top).toBe(550);

  moveTo(100);
  grow();
  expect(top).toBe(100);
  moveTo(height);
  expect(scroll.isFollowing()).toBe(true);
  grow();
  expect(top).toBe(650);

  scroll.dispose();
  expect(disconnect).toHaveBeenCalledOnce();
  expect(viewport.dataset.followingOutput).toBeUndefined();
});
