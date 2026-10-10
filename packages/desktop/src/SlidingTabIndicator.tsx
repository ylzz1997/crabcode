import { useLayoutEffect, useRef, useState } from "react";

export function SlidingTabIndicator({ activeKey, className = "" }: { activeKey: string; className?: string }) {
  const indicatorRef = useRef<HTMLSpanElement>(null);
  const [position, setPosition] = useState<{ left: number; width: number } | null>(null);

  useLayoutEffect(() => {
    const tablist = indicatorRef.current?.parentElement;
    if (!tablist) return;
    const measure = () => {
      const tab = tablist.querySelector<HTMLButtonElement>('[role="tab"][aria-selected="true"]');
      if (!tab) return;
      const next = { left: tab.offsetLeft, width: tab.offsetWidth };
      setPosition((current) => current?.left === next.left && current.width === next.width ? current : next);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(tablist);
    for (const tab of tablist.querySelectorAll('[role="tab"]')) observer.observe(tab);
    return () => observer.disconnect();
  }, [activeKey]);

  return <span ref={indicatorRef} className={`sliding-tab-indicator ${className}`} aria-hidden="true"
    style={{ width: position?.width ?? 0, transform: `translateX(${position?.left ?? 0}px)`, visibility: position ? undefined : "hidden" }} />;
}
