import { getLocale } from "./i18n";
import { t as tr } from "./i18n";
import { AlertTriangle, Check, ChevronUp, Clock, Folder, LoaderCircle, Maximize2, MonitorUp, MousePointer2, Power, RefreshCw, Server, Terminal, WifiOff, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ComputerUsePreview, ComputerUseState } from "./computerUse";
import { computerUseReauthorizationHint } from "./computerUse";
import type { GatewayStartupState } from "./gatewayStartup";
import type { ConnectionPreset, GatewayViewState, ProjectPreset, RuntimeSettingsResponse } from "./types";

interface StatusBarProps {
  connection?: ConnectionPreset | null;
  gateway?: GatewayViewState | null;
  startup?: GatewayStartupState;
  project?: ProjectPreset | null;
  loading?: boolean;
  error?: string | null;
  activity?: string | null;
  onRetry?: () => void;
  onConnections?: () => void;
  computerUse?: ComputerUseState;
  computerUseConfig?: RuntimeSettingsResponse | null;
  onComputerUseEnabledChange?: (enabled: boolean) => void;
  onComputerUseOpenInputSettings?: (kind: "screen_recording" | "accessibility") => void;
  onComputerUseRefresh?: () => void;
}

function previewIdentity(preview: ComputerUsePreview, index: number): string {
  const value = preview.agentId || preview.sessionId;
  const prefix = preview.agentId ? "Agent" : tr("会话");
  return value ? `${prefix} ${value.length > 16 ? `${value.slice(0, 6)}…${value.slice(-4)}` : value}` : tr("任务 {0}", { 0: index + 1 });
}

function previewCursorPosition(preview: ComputerUsePreview | null): { left: number; top: number } {
  if (!preview?.frame || !preview.cursor) return { left: -1, top: -1 };
  const { frame, cursor, mode } = preview;
  // Background coordinates are already window-local; desktop coordinates are absolute.
  const originX = mode === "background_app" ? 0 : frame.origin_x;
  const originY = mode === "background_app" ? 0 : frame.origin_y;
  return {
    left: (cursor.x - originX) / frame.width * 100,
    top: (cursor.y - originY) / frame.height * 100,
  };
}

export function StatusBar({ connection, gateway, startup, project, loading, error, activity, onRetry, onConnections, computerUse, computerUseConfig, onComputerUseEnabledChange, onComputerUseOpenInputSettings, onComputerUseRefresh }: StatusBarProps) {
  const [expanded, setExpanded] = useState(false);
  const [computerExpanded, setComputerExpanded] = useState(false);
  const [computerDetail, setComputerDetail] = useState<ComputerUsePreview | null>(null);
  const [now, setNow] = useState(Date.now);
  const [mountedAt] = useState(Date.now);
  const logRef = useRef<HTMLDivElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const computerToggleRef = useRef<HTMLButtonElement>(null);
  const computerDetailCloseRef = useRef<HTMLButtonElement>(null);
  const computerDetailTriggerRef = useRef<HTMLElement | null>(null);
  const computerWasActiveRef = useRef(computerUse?.active === true);
  const failed = Boolean(error || gateway?.status === "error");
  const busy = !failed && Boolean(loading || activity || (connection && (!gateway || gateway.status === "connecting")));
  const status = failed ? "error" : busy ? "busy" : gateway?.status === "online" ? "online" : "offline";
  const detail = error || gateway?.error || activity || (loading ? tr("正在读取桌面配置…")
    : busy ? startup?.detail || tr("正在连接 Gateway…")
    : gateway?.status === "online" ? tr("就绪") : tr("尚未连接 Gateway"));
  const elapsed = Math.max(0, Math.floor(((startup?.finishedAt ?? now) - (startup?.startedAt ?? mountedAt)) / 1000));
  const duration = elapsed < 60 ? tr("{0} 秒", { 0: elapsed }) : tr("{0} 分 {1} 秒", { 0: Math.floor(elapsed / 60), 1: elapsed % 60 });

  useEffect(() => {
    if (!busy) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [busy]);

  useEffect(() => {
    if (expanded && logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [expanded, startup?.history, activity]);

  useEffect(() => {
    const wasActive = computerWasActiveRef.current;
    const isActive = computerUse?.active === true;
    computerWasActiveRef.current = isActive;
    if (wasActive && !isActive) setComputerExpanded(false);
  }, [computerUse?.active]);

  useEffect(() => {
    if (!computerDetail) return;
    const current = computerUse?.previews.find((preview) => preview.key === computerDetail.key);
    if (current && current !== computerDetail) setComputerDetail(current);
  }, [computerDetail, computerUse?.previews]);

  useEffect(() => {
    if (!computerDetail) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    computerDetailCloseRef.current?.focus();
    return () => {
      document.body.style.overflow = previousOverflow;
      computerDetailTriggerRef.current?.focus();
    };
  }, [computerDetail?.key]);

  const close = () => {
    setExpanded(false);
    toggleRef.current?.focus();
  };
  const configuredMode = computerUseConfig?.computer_use_target_scope === "desktop" ? "foreground_desktop"
    : computerUseConfig?.computer_use_target_scope === "app_window" ? "background_app"
      : computerUseConfig?.computer_use_mode ?? computerUse?.mode;
  const configuredPolicy = computerUseConfig?.computer_use_delivery_policy ?? computerUse?.deliveryPolicy;
  const strictInputUnavailable = configuredPolicy === "strict_background"
    && computerUse?.capabilities?.strict_background_input_available === false;
  const computerStatusLabel = computerUse?.status === "ready"
    ? computerUse.capabilities?.input_available === false || computerUse.capabilities?.capture_available === false || strictInputUnavailable ? tr("有限可用") : tr("可用")
    : computerUse?.status === "busy" ? tr("Agent 正在操作")
      : computerUse?.status === "connecting" ? tr("正在连接")
        : computerUse?.status === "unavailable" ? tr("不可用")
          : computerUse?.status === "error" ? tr("连接错误") : tr("已关闭");
  const previews = computerUse?.previews ?? [];
  const needsMacInputPermission = computerUse?.enabled
    && computerUse.capabilities?.platform === "macos"
    && computerUse.capabilities.input_available === false;
  const inputReauthorizationHint = tr(computerUseReauthorizationHint(computerUse?.capabilities, "accessibility"));
  const captureReauthorizationHint = tr(computerUseReauthorizationHint(computerUse?.capabilities, "screen_recording"));
  const detailLogs = computerDetail ? (computerUse?.logs ?? []).filter(
    (entry) => entry.sessionId === computerDetail.sessionId && entry.agentId === computerDetail.agentId,
  ) : [];
  const detailFrame = computerDetail?.frame;
  const detailCursor = computerDetail?.cursor;
  const { left: detailCursorLeft, top: detailCursorTop } = previewCursorPosition(computerDetail);

  return (
    <footer className={`desktop-status-bar ${status}`} aria-label={tr("应用状态栏")} onKeyDown={(event) => {
      if (event.key === "Escape" && (expanded || computerExpanded)) {
        event.stopPropagation();
        if (computerExpanded) {
          setComputerExpanded(false);
          computerToggleRef.current?.focus();
        } else close();
      }
    }}>
      {expanded && (
        <section className="startup-details" id="startup-details" aria-label={tr("启动详情")}>
          <header>
            <strong><Terminal />{connection?.name ?? "Crab Desktop"}{tr(" · 启动详情")}</strong>
            <span>{startup ? tr("耗时 {0}", { 0: duration }) : ""}</span>
            {failed && onRetry && <button className="status-retry" onClick={onRetry}><RefreshCw />{connection ? tr("重试连接") : tr("重新加载")}</button>}
            <button className="icon-button tiny" aria-label={tr("关闭启动详情")} onClick={close}><X /></button>
          </header>
          <div className="startup-log" ref={logRef}>
            {startup?.history.length ? startup.history.map((entry, index) => (
              <div className="startup-log-line" key={`${entry.time}-${index}`}>
                <time>{new Date(entry.time).toLocaleTimeString(getLocale(), { hour12: false })}</time>
                <span>{entry.detail}</span>
              </div>
            )) : <p>{detail}</p>}
            {activity && startup?.history.length ? <p role="status">{activity}</p> : null}
          </div>
        </section>
      )}
      {computerExpanded && computerUse && (
        <section className={`computer-use-console ${previews.length > 1 ? "multi" : ""}`} id="computer-use-console" aria-label={tr("Computer Use 控制台")}>
          <header>
            <strong><MonitorUp />Computer Use{computerUse.capabilities?.environment === "local_vm" ? tr(" · {0} · 独立桌面", { 0: computerUse.capabilities.environment_name }) : ""}</strong>
            <span className="computer-use-mode">{previews.length > 1
              ? tr("{0} 个活动预览", { 0: previews.length })
              : computerUse.capabilities?.environment === "local_vm" ? tr("虚拟机独立桌面") : computerUseLabel(configuredMode ?? computerUse.mode, configuredPolicy)}</span>
            <span className={`computer-use-state ${computerUse.status}`}>{computerStatusLabel}</span>
            <button
              className={`computer-use-power ${computerUse.enabled ? "enabled" : ""}`}
              onClick={() => onComputerUseEnabledChange?.(!computerUse.enabled)}
              title={computerUse.enabled ? tr("关闭 Computer Use") : tr("开启 Computer Use")}
            >
              <Power />{computerUse.enabled ? tr("关闭") : tr("开启")}
            </button>
            <button className="icon-button tiny" aria-label={tr("关闭 Computer Use 控制台")} onClick={() => setComputerExpanded(false)}><X /></button>
          </header>
          <div className="computer-use-preview">
            {previews.length ? previews.map((preview, index) => {
              const frame = preview.frame;
              const cursor = preview.cursor;
              const { left: cursorLeft, top: cursorTop } = previewCursorPosition(preview);
              const identity = previewIdentity(preview, index);
              return (
                <section className={`computer-use-preview-card ${preview.status}`} key={preview.key} aria-label={tr("{0} Computer Use 预览", { 0: identity })}>
                  <header>
                    <strong title={preview.agentId || preview.sessionId}>{identity}</strong>
                    <span>{computerUse.capabilities?.environment === "local_vm" ? tr("虚拟机独立桌面") : tr("操作时：{0}", { 0: computerUseLabel(preview.mode, preview.deliveryPolicy) })}</span>
                    <code>{preview.action}</code>
                    {preview.observationKind?.startsWith("ax") && <span>{tr("辅助功能 · ")}{preview.axElementCount ?? 0}{tr(" 个元素")}</span>}
                    <em>{preview.status === "busy" ? tr("正在操作") : preview.status === "error" ? tr("失败") : tr("等待后续")}</em>
                  </header>
                  <button
                    type="button"
                    className="computer-use-preview-content"
                    aria-label={tr("查看 {0} Computer Use 详情", { 0: identity })}
                    onClick={(event) => {
                      computerDetailTriggerRef.current = event.currentTarget;
                      setComputerDetail(preview);
                    }}
                  >
                    {frame ? (
                      <div className="computer-use-frame">
                        <img src={`data:${frame.media_type};base64,${frame.data}`} alt={tr("{0} 最近一次桌面截图", { 0: identity })} />
                        {preview.observationKind === "ax" && <span className="computer-use-frame-age">{tr("监看截图")}{preview.frameUpdatedAt ? ` · ${new Date(preview.frameUpdatedAt).toLocaleTimeString(getLocale(), { hour12: false })}` : ""}{tr("；本次模型读取 AX Tree")}</span>}
                        {cursor && cursorLeft >= 0 && cursorLeft <= 100 && cursorTop >= 0 && cursorTop <= 100 && (
                          <MousePointer2
                            className="computer-use-cursor"
                            style={{ left: `${cursorLeft}%`, top: `${cursorTop}%` }}
                            aria-label={tr("光标 {0}, {1}", { 0: cursor.x, 1: cursor.y })}
                          />
                        )}
                        <span className="computer-use-preview-expand" aria-hidden="true"><Maximize2 /></span>
                      </div>
                    ) : (
                      <div className="computer-use-empty compact">
                        <MonitorUp />
                        <span>{tr("暂无截图")}</span>
                      </div>
                    )}
                  </button>
                </section>
              );
            }) : (
              <div className="computer-use-preview-empty">
                <div className="computer-use-empty">
                  <MonitorUp />
                  <span>{computerUse.enabled
                    ? needsMacInputPermission ? tr("等待 Agent 开始操作电脑") : computerUse.error || tr("等待 Agent 开始操作电脑")
                    : tr("Computer Use 已关闭，不会向 Agent 暴露相关工具")}</span>
                </div>
              </div>
            )}
          </div>
          {needsMacInputPermission && (
            <section className="computer-use-permission" aria-label={tr("需要辅助功能权限")}>
              <AlertTriangle />
              <div>
                <strong>{inputReauthorizationHint ? tr("需要重新授权辅助功能") : tr("需要开启辅助功能权限")}</strong>
                {inputReauthorizationHint && <span>{inputReauthorizationHint}</span>}
                <span>{computerUse.capabilities?.environment === "local_vm" ? tr("请在虚拟机内为 Crab Computer Use 授予辅助功能和录屏权限。宿主机权限不影响虚拟机输入。") : tr("点击、输入和滚动需要在 macOS“系统设置 → 隐私与安全性 → 辅助功能”中允许 Crab Computer Use。点击“打开系统设置”后，可将弹出的应用卡片拖入辅助功能列表。当前仍可查看屏幕。")}</span>
              </div>
              <div className="computer-use-permission-actions">
                <button type="button" onClick={() => onComputerUseOpenInputSettings?.("accessibility")}>{tr("打开系统设置")}</button>
                <button type="button" onClick={onComputerUseRefresh}>{tr("重新检测")}</button>
              </div>
            </section>
          )}
          {computerUse.enabled && computerUse.capabilities?.input_available === false && !needsMacInputPermission && (
            <p className="computer-use-warning">{computerUse.capabilities.reason || tr("桌面输入权限不可用；Agent 仍可查看屏幕。")}</p>
          )}
          {computerUse.enabled && strictInputUnavailable && (
            <p className="computer-use-warning">{tr("当前宿主尚不支持严格后台输入；可以查看窗口，但输入动作会被拒绝。若要操作，请在设置中选择允许前台操作。")}</p>
          )}
          {computerUse.enabled && computerUse.capabilities?.capture_available === false && (
            <section className="computer-use-permission" aria-label={tr("录屏不可用")}>
              <AlertTriangle />
              <div>
                <strong>{captureReauthorizationHint ? tr("需要重新授权录屏") : tr("录屏不可用")}</strong>
                {captureReauthorizationHint && <span>{captureReauthorizationHint}</span>}
                <span>{computerUse.capabilities.environment === "local_vm"
                  ? tr("请在虚拟机内为 Crab Computer Use 授予录屏权限，再重新检测。宿主机授权不能代替虚拟机内的授权。")
                  : computerUse.capabilities.platform === "macos"
                  ? tr("窗口列表和截图需要录屏权限。点击“打开系统设置”，将弹出的 Crab Computer Use.app 卡片拖到录屏列表并打开开关；也可点＋，按 ⌘⇧G 输入 ~/Library/Application Support/CrabCode/ComputerUse/Crab Computer Use.app 后添加。Crab Desktop 或 VS Code 的授权不能代替此组件的授权。")
                  : computerUse.capabilities.reason || tr("请检查屏幕捕获权限并重新检测。")}</span>
              </div>
              <div className="computer-use-permission-actions">
                {computerUse.capabilities.platform === "macos" && <button type="button" onClick={() => onComputerUseOpenInputSettings?.("screen_recording")}>{tr("打开系统设置")}</button>}
                <button type="button" onClick={onComputerUseRefresh}>{tr("重新检测")}</button>
              </div>
            </section>
          )}
          <div className="computer-use-log" aria-label={tr("Agent 操作记录")}>
            {computerUse.logs.length ? [...computerUse.logs].reverse().map((entry) => (
              <div className={`computer-use-log-line ${entry.ok ? "ok" : "failed"}`} key={entry.id}>
                <time>{new Date(entry.time).toLocaleTimeString(getLocale(), { hour12: false })}</time>
                <code>{entry.action}</code>
                <span>{entry.summary}</span>
              </div>
            )) : <p>{tr("还没有 Computer Use 操作。")}</p>}
          </div>
        </section>
      )}
      {connection && (
        <button className="status-connection" onClick={onConnections} title={tr("管理连接 · {0}", { 0: connection.name })}>
          <Server /><span>{connection.name}</span>
        </button>
      )}
      <button
        className="status-current"
        ref={toggleRef}
        onClick={() => {
          setComputerExpanded(false);
          setExpanded((value) => !value);
        }}
        aria-expanded={expanded}
        aria-controls="startup-details"
        title={tr("{0}\n点击查看启动详情", { 0: detail })}
      >
        {busy ? <LoaderCircle className="spin" /> : failed ? <AlertTriangle /> : status === "online" ? <Check /> : <WifiOff />}
        <span className="status-message" role="status" aria-live="polite">{detail}</span>
        {busy && !activity && <span className="status-elapsed"><Clock />{duration}</span>}
        <ChevronUp className={`status-expand ${expanded ? "expanded" : ""}`} />
      </button>
      {failed && onRetry && <button className="status-retry" onClick={onRetry} title={connection ? tr("重新连接 Gateway") : tr("重新加载桌面配置")}><RefreshCw />{tr("重试")}</button>}
      <div className="status-spacer" />
      {project && <span className="status-project" title={project.path}><Folder />{project.name}</span>}
      {computerUse && (
        <button
          className={`status-computer-use ${computerUse.status}`}
          ref={computerToggleRef}
          onClick={() => {
            setExpanded(false);
            setComputerExpanded((value) => !value);
          }}
          aria-expanded={computerExpanded}
          aria-controls="computer-use-console"
          title={tr("Computer Use · {0}\n点击查看 Agent 对电脑的操作", { 0: computerStatusLabel })}
        >
          <span>Computer Use</span>
          <span className="computer-use-dot" />
        </button>
      )}
      {computerDetail && createPortal(
        <div
          className="computer-use-detail-backdrop"
          role="dialog"
          aria-modal="true"
          aria-label={tr("{0} Computer Use 详情", { 0: previewIdentity(computerDetail, 0) })}
          onMouseDown={(event) => event.target === event.currentTarget && setComputerDetail(null)}
          onKeyDown={(event) => {
            event.stopPropagation();
            if (event.key === "Escape") setComputerDetail(null);
          }}
        >
          <section className="computer-use-detail">
            <header>
              <div>
                <strong><MonitorUp />{previewIdentity(computerDetail, 0)}</strong>
                <span>{computerDetail.summary}</span>
              </div>
              <div className="computer-use-detail-badges">
                <span>{computerUse?.capabilities?.environment === "local_vm" ? tr("虚拟机独立桌面") : tr("操作时：{0}", { 0: computerUseLabel(computerDetail.mode, computerDetail.deliveryPolicy) })}</span>
                <code>{computerDetail.action}</code>
                {computerDetail.observationKind?.startsWith("ax") && <span>{tr("辅助功能 · ")}{computerDetail.axElementCount ?? 0}{tr(" 个元素")}</span>}
                <em className={computerDetail.status}>{computerDetail.status === "busy" ? tr("正在操作") : computerDetail.status === "error" ? tr("失败") : tr("等待后续")}</em>
              </div>
              <button
                ref={computerDetailCloseRef}
                type="button"
                aria-label={tr("关闭 Computer Use 详情")}
                title={tr("关闭 Computer Use 详情")}
                onClick={() => setComputerDetail(null)}
              ><X /></button>
            </header>
            <div className="computer-use-detail-body">
              <div className="computer-use-detail-image">
                {detailFrame ? (
                  <div className="computer-use-detail-frame">
                    <img src={`data:${detailFrame.media_type};base64,${detailFrame.data}`} alt={tr("{0} Computer Use 完整截图", { 0: previewIdentity(computerDetail, 0) })} />
                    {computerDetail.observationKind === "ax" && <span className="computer-use-frame-age">{tr("监看截图")}{computerDetail.frameUpdatedAt ? ` · ${new Date(computerDetail.frameUpdatedAt).toLocaleTimeString(getLocale(), { hour12: false })}` : ""}{tr("；本次模型读取 AX Tree")}</span>}
                    {detailCursor && detailCursorLeft >= 0 && detailCursorLeft <= 100 && detailCursorTop >= 0 && detailCursorTop <= 100 && (
                      <MousePointer2
                        className="computer-use-cursor"
                        style={{ left: `${detailCursorLeft}%`, top: `${detailCursorTop}%` }}
                        aria-label={tr("光标 {0}, {1}", { 0: detailCursor.x, 1: detailCursor.y })}
                      />
                    )}
                  </div>
                ) : (
                  <div className="computer-use-empty"><MonitorUp /><span>{tr("暂无截图")}</span></div>
                )}
              </div>
              <aside className="computer-use-detail-sidebar">
                <dl>
                  <div><dt>{tr("会话")}</dt><dd title={computerDetail.sessionId}>{computerDetail.sessionId || tr("未知")}</dd></div>
                  <div><dt>Agent</dt><dd title={computerDetail.agentId}>{computerDetail.agentId || tr("主 Agent")}</dd></div>
                  <div><dt>{tr("更新时间")}</dt><dd>{new Date(computerDetail.updatedAt).toLocaleTimeString(getLocale(), { hour12: false })}</dd></div>
                  {detailFrame && <div><dt>{tr("截图")}</dt><dd>{detailFrame.width} × {detailFrame.height} · ({detailFrame.origin_x}, {detailFrame.origin_y})</dd></div>}
                </dl>
                <section>
                  <h3>{tr("操作记录")}</h3>
                  <div className="computer-use-detail-log">
                    {detailLogs.length ? [...detailLogs].reverse().map((entry) => (
                      <div className={entry.ok ? "ok" : "failed"} key={entry.id}>
                        <time>{new Date(entry.time).toLocaleTimeString(getLocale(), { hour12: false })}</time>
                        <code>{entry.action}</code>
                        <span>{entry.summary}</span>
                      </div>
                    )) : <p>{tr("暂无操作记录。")}</p>}
                  </div>
                </section>
              </aside>
            </div>
          </section>
        </div>,
        document.body,
      )}
    </footer>
  );
}
function computerUseLabel(mode: string, policy?: string): string {
  const scope = mode === "background_app" ? tr("指定窗口") : tr("整个桌面");
  return `${scope} · ${policy === "allow_foreground" ? tr("允许前台") : policy === "strict_background" ? tr("严格后台") : tr("权限未声明")}`;
}
