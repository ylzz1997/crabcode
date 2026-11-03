import type { UnlistenFn } from "@tauri-apps/api/event";
import { useEffect, useLayoutEffect, useState, type CSSProperties, type ReactNode } from "react";
import { t as tr } from "./i18n";
import { isDesktopShell } from "./native";
import { isMacOSPlatform, isWindowsPlatform } from "./platform";

export function WindowTitlebar({ children, sidebarWidth = 0 }: { children?: ReactNode; sidebarWidth?: number }) {
  const platform = !isDesktopShell() ? "browser"
    : isMacOSPlatform() ? "macos"
      : isWindowsPlatform() ? "windows" : "native";
  const integrated = platform === "macos" || platform === "windows";
  const [maximized, setMaximized] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  const [focused, setFocused] = useState(true);

  useLayoutEffect(() => {
    const root = document.documentElement;
    const previousPlatform = root.dataset.windowPlatform;
    const previousFullscreen = root.dataset.windowFullscreen;
    root.dataset.windowPlatform = platform;
    root.dataset.windowFullscreen = String(fullscreen);
    return () => {
      if (previousPlatform === undefined) delete root.dataset.windowPlatform;
      else root.dataset.windowPlatform = previousPlatform;
      if (previousFullscreen === undefined) delete root.dataset.windowFullscreen;
      else root.dataset.windowFullscreen = previousFullscreen;
    };
  }, [platform, fullscreen]);

  useEffect(() => {
    if (!integrated) return;
    let disposed = false;
    const unlisteners: UnlistenFn[] = [];
    const track = async (listener: Promise<UnlistenFn>) => {
      const unlisten = await listener;
      if (disposed) unlisten();
      else unlisteners.push(unlisten);
    };
    const watchWindow = async () => {
      const { getCurrentWindow } = await import("@tauri-apps/api/window");
      if (disposed) return;
      const appWindow = getCurrentWindow();
      const refresh = async () => {
        const [nextMaximized, nextFullscreen, nextFocused] = await Promise.all([
          appWindow.isMaximized(), appWindow.isFullscreen(), appWindow.isFocused(),
        ]);
        if (disposed) return;
        setMaximized(nextMaximized);
        setFullscreen(nextFullscreen);
        setFocused(nextFocused);
      };
      const refreshWindow = () => { void refresh().catch(console.error); };
      await Promise.all([
        refresh(),
        track(appWindow.onResized(refreshWindow)),
        track(appWindow.onFocusChanged(refreshWindow)),
      ]);
    };
    void watchWindow().catch(console.error);
    return () => {
      disposed = true;
      unlisteners.forEach((unlisten) => unlisten());
    };
  }, [integrated]);

  const controlWindow = (action: "minimize" | "toggleMaximize" | "close") => {
    void import("@tauri-apps/api/window").then(({ getCurrentWindow }) => getCurrentWindow()[action]()).catch(console.error);
  };

  return (
    <header
      className="window-titlebar"
      style={{ "--titlebar-sidebar-width": `${sidebarWidth}px` } as CSSProperties}
      data-platform={platform}
      data-fullscreen={fullscreen}
      data-focused={focused}
      data-has-content={Boolean(children)}
      data-tauri-drag-region={integrated ? true : undefined}
    >
      {children}
      <div className="window-titlebar-spacer" data-tauri-drag-region={integrated ? true : undefined} />
      {platform === "windows" && (
        <div className="window-controls">
          <button type="button" className="window-control" aria-label={tr("最小化")} title={tr("最小化")} onClick={() => controlWindow("minimize")}>
            <svg viewBox="0 0 12 12" aria-hidden="true"><path d="M1 6.5h10" /></svg>
          </button>
          <button type="button" className="window-control" aria-label={maximized ? tr("还原窗口") : tr("最大化")} title={maximized ? tr("还原窗口") : tr("最大化")} onClick={() => controlWindow("toggleMaximize")}>
            <svg viewBox="0 0 12 12" aria-hidden="true">
              {maximized ? <path d="M3.5 3.5v-2h7v7h-2m-7-5h7v7h-7z" /> : <rect x="1.5" y="1.5" width="9" height="9" />}
            </svg>
          </button>
          <button type="button" className="window-control window-control-close" aria-label={tr("关闭窗口")} title={tr("关闭窗口")} onClick={() => controlWindow("close")}>
            <svg viewBox="0 0 12 12" aria-hidden="true"><path d="m1.5 1.5 9 9m0-9-9 9" /></svg>
          </button>
        </div>
      )}
    </header>
  );
}
