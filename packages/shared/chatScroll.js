export function bindChatScroll(viewport, content) {
  const isFollowing = () => viewport.dataset.followingOutput === "true";
  const setFollowing = (following) => {
    const value = String(following);
    if (viewport.dataset.followingOutput !== value) viewport.dataset.followingOutput = value;
  };
  const isAtBottom = () => viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop <= 2;
  let previousTop = viewport.scrollTop;

  function onScroll() {
    const top = viewport.scrollTop;
    if (isAtBottom()) {
      setFollowing(true);
    } else if (top < previousTop) {
      // Content can grow before a pending scroll event from the last update.
      // Keep following unless the viewport actually moved up.
      setFollowing(false);
    }
    previousTop = top;
  }

  function syncLayout() {
    if (isFollowing()) {
      viewport.scrollTop = viewport.scrollHeight;
      previousTop = viewport.scrollTop;
    } else if (isAtBottom()) {
      setFollowing(true);
    }
  }

  function jumpToBottom() {
    setFollowing(true);
    syncLayout();
  }

  viewport.addEventListener("scroll", onScroll, { passive: true });
  const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(syncLayout);
  observer?.observe(viewport);
  observer?.observe(content);
  jumpToBottom();

  return {
    isFollowing,
    jumpToBottom,
    dispose() {
      observer?.disconnect();
      viewport.removeEventListener("scroll", onScroll);
      delete viewport.dataset.followingOutput;
    },
  };
}
