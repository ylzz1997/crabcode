import { AlertTriangle, Check, ChevronUp, Clock, Folder, LoaderCircle, Maximize2, MonitorUp, MousePointer2, Power, RefreshCw, Server, Terminal, WifiOff, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ComputerUsePreview, ComputerUseState } from "./computerUse";
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
  onComputerUseOpenInputSettings?: () => void;
  onComputerUseRefresh?: () => void;
}

function previewIdentity(preview: ComputerUsePreview, index: number): string {
  const value = preview.agentId || preview.sessionId;
  const prefix = preview.agentId ? "Agent" : "会话";
  return value ? `${prefix} ${value.length > 16 ? `${value.slice(0, 6)}…${value.slice(-4)}` : value}` : `任务 ${index + 1}`;
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
  const detail = error || gateway?.error || activity || (loading ? "正在读取桌面配置…"
    : busy ? startup?.detail || "正在连接 Gateway…"
    : gateway?.status === "online" ? "就绪" : "尚未连接 Gateway");
  const elapsed = Math.max(0, Math.floor(((startup?.finishedAt ?? now) - (startup?.startedAt ?? mountedAt)) / 1000));
  const duration = elapsed < 60 ? `${elapsed} 秒` : `${Math.floor(elapsed / 60)} 分 ${elapsed % 60} 秒`;

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
    ? computerUse.capabilities?.input_available === false || computerUse.capabilities?.capture_available === false || strictInputUnavailable ? "有限可用" : "可用"
    : computerUse?.status === "busy" ? "Agent 正在操作"
      : computerUse?.status === "connecting" ? "正在连接"
        : computerUse?.status === "unavailable" ? "不可用"
          : computerUse?.status === "error" ? "连接错误" : "已关闭";
  const previews = computerUse?.previews ?? [];
  const needsMacInputPermission = computerUse?.enabled
    && computerUse.capabilities?.platform === "macos"
    && computerUse.capabilities.input_available === false;
  const detailLogs = computerDetail ? (computerUse?.logs ?? []).filter(
    (entry) => entry.sessionId === computerDetail.sessionId && entry.agentId === computerDetail.agentId,
  ) : [];
  const detailFrame = computerDetail?.frame;
  const detailCursor = computerDetail?.cursor;
  const { left: detailCursorLeft, top: detailCursorTop } = previewCursorPosition(computerDetail);

  return (
    <footer className={`desktop-status-bar ${status}`} aria-label="应用状态栏" onKeyDown={(event) => {
      if (event.key === "Escape" && (expanded || computerExpanded)) {
        event.stopPropagation();
        if (computerExpanded) {
          setComputerExpanded(false);
          computerToggleRef.current?.focus();
        } else close();
      }
    }}>
      {expanded && (
        <section className="startup-details" id="startup-details" aria-label="启动详情">
          <header>
            <strong><Terminal />{connection?.name ?? "Crab Desktop"} · 启动详情</strong>
            <span>{startup ? `耗时 ${duration}` : ""}</span>
            {failed && onRetry && <button className="status-retry" onClick={onRetry}><RefreshCw />{connection ? "重试连接" : "重新加载"}</button>}
            <button className="icon-button tiny" aria-label="关闭启动详情" onClick={close}><X /></button>
          </header>
          <div className="startup-log" ref={logRef}>
            {startup?.history.length ? startup.history.map((entry, index) => (
              <div className="startup-log-line" key={`${entry.time}-${index}`}>
                <time>{new Date(entry.time).toLocaleTimeString("zh-CN", { hour12: false })}</time>
                <span>{entry.detail}</span>
              </div>
            )) : <p>{detail}</p>}
            {activity && startup?.history.length ? <p role="status">{activity}</p> : null}
          </div>
        </section>
      )}
      {computerExpanded && computerUse && (
        <section className={`computer-use-console ${previews.length > 1 ? "multi" : ""}`} id="computer-use-console" aria-label="Computer Use 控制台">
          <header>
            <strong><MonitorUp />Computer Use{computerUse.capabilities?.environment === "local_vm" ? ` · ${computerUse.capabilities.environment_name} · 独立桌面` : ""}</strong>
            <span className="computer-use-mode">{previews.length > 1
              ? `${previews.length} 个活动预览`
              : computerUse.capabilities?.environment === "local_vm" ? "虚拟机独立桌面" : computerUseLabel(configuredMode ?? computerUse.mode, configuredPolicy)}</span>
            <span className={`computer-use-state ${computerUse.status}`}>{computerStatusLabel}</span>
            <button
              className={`computer-use-power ${computerUse.enabled ? "enabled" : ""}`}
              onClick={() => onComputerUseEnabledChange?.(!computerUse.enabled)}
              title={computerUse.enabled ? "关闭 Computer Use" : "开启 Computer Use"}
            >
              <Power />{computerUse.enabled ? "关闭" : "开启"}
            </button>
            <button className="icon-button tiny" aria-label="关闭 Computer Use 控制台" onClick={() => setComputerExpanded(false)}><X /></button>
          </header>
          <div className="computer-use-preview">
            {previews.length ? previews.map((preview, index) => {
              const frame = preview.frame;
              const cursor = preview.cursor;
              const { left: cursorLeft, top: cursorTop } = previewCursorPosition(preview);
              const identity = previewIdentity(preview, index);
              return (
                <section className={`computer-use-preview-card ${preview.status}`} key={preview.key} aria-label={`${identity} Computer Use 预览`}>
                  <header>
                    <strong title={preview.agentId || preview.sessionId}>{identity}</strong>
                    <span>{computerUse.capabilities?.environment === "local_vm" ? "虚拟机独立桌面" : `操作时：${computerUseLabel(preview.mode, preview.deliveryPolicy)}`}</span>
                    <code>{preview.action}</code>
                    {preview.observationKind?.startsWith("ax") && <span>辅助功能 · {preview.axElementCount ?? 0} 个元素</span>}
                    <em>{preview.status === "busy" ? "正在操作" : preview.status === "error" ? "失败" : "等待后续"}</em>
                  </header>
                  <button
                    type="button"
                    className="computer-use-preview-content"
                    aria-label={`查看 ${identity} Computer Use 详情`}
                    onClick={(event) => {
                      computerDetailTriggerRef.current = event.currentTarget;
                      setComputerDetail(preview);
                    }}
                  >
                    {frame ? (
                      <div className="computer-use-frame">
                        <img src={`data:${frame.media_type};base64,${frame.data}`} alt={`${identity} 最近一次桌面截图`} />
                        {preview.observationKind === "ax" && <span className="computer-use-frame-age">监看截图{preview.frameUpdatedAt ? ` · ${new Date(preview.frameUpdatedAt).toLocaleTimeString("zh-CN", { hour12: false })}` : ""}；本次模型读取 AX Tree</span>}
                        {cursor && cursorLeft >= 0 && cursorLeft <= 100 && cursorTop >= 0 && cursorTop <= 100 && (
                          <MousePointer2
                            className="computer-use-cursor"
                            style={{ left: `${cursorLeft}%`, top: `${cursorTop}%` }}
                            aria-label={`光标 ${cursor.x}, ${cursor.y}`}
                          />
                        )}
                        <span className="computer-use-preview-expand" aria-hidden="true"><Maximize2 /></span>
                      </div>
                    ) : (
                      <div className="computer-use-empty compact">
                        <MonitorUp />
                        <span>暂无截图</span>
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
                    ? needsMacInputPermission ? "等待 Agent 开始操作电脑" : computerUse.error || "等待 Agent 开始操作电脑"
                    : "Computer Use 已关闭，不会向 Agent 暴露相关工具"}</span>
                </div>
              </div>
            )}
          </div>
          {needsMacInputPermission && (
            <section className="computer-use-permission" aria-label="需要辅助功能权限">
              <AlertTriangle />
              <div>
                <strong>需要开启辅助功能权限</strong>
                <span>{computerUse.capabilities?.environment === "local_vm" ? "请在虚拟机内为 Crab Computer Use 授予辅助功能和录屏权限。宿主机权限不影响虚拟机输入。" : "点击、输入和滚动需要在 macOS“系统设置 → 隐私与安全性 → 辅助功能”中允许 Crab Desktop，同时需要前台权限策略支持。当前仍可查看屏幕。"}</span>
              </div>
              <div className="computer-use-permission-actions">
                <button type="button" onClick={onComputerUseOpenInputSettings}>打开系统设置</button>
                <button type="button" onClick={onComputerUseRefresh}>重新检测</button>
              </div>
            </section>
          )}
          {computerUse.enabled && computerUse.capabilities?.input_available === false && !needsMacInputPermission && (
            <p className="computer-use-warning">{computerUse.capabilities.reason || "桌面输入权限不可用；Agent 仍可查看屏幕。"}</p>
          )}
          {computerUse.enabled && strictInputUnavailable && (
            <p className="computer-use-warning">当前宿主尚不支持严格后台输入；可以查看窗口，但输入动作会被拒绝。若要操作，请在设置中选择允许前台操作。</p>
          )}
          {computerUse.enabled && computerUse.capabilities?.ax_available && computerUse.capabilities.capture_available === false && (
            <p className="computer-use-warning">辅助功能可用；录屏不可用时仍可读取和操作界面元素，截图操作需要录屏权限。</p>
          )}
          <div className="computer-use-log" aria-label="Agent 操作记录">
            {computerUse.logs.length ? [...computerUse.logs].reverse().map((entry) => (
              <div className={`computer-use-log-line ${entry.ok ? "ok" : "failed"}`} key={entry.id}>
                <time>{new Date(entry.time).toLocaleTimeString("zh-CN", { hour12: false })}</time>
                <code>{entry.action}</code>
                <span>{entry.summary}</span>
              </div>
            )) : <p>还没有 Computer Use 操作。</p>}
          </div>
        </section>
      )}
      {connection && (
        <button className="status-connection" onClick={onConnections} title={`管理连接 · ${connection.name}`}>
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
        title={`${detail}\n点击查看启动详情`}
      >
        {busy ? <LoaderCircle className="spin" /> : failed ? <AlertTriangle /> : status === "online" ? <Check /> : <WifiOff />}
        <span className="status-message" role="status" aria-live="polite">{detail}</span>
        {busy && !activity && <span className="status-elapsed"><Clock />{duration}</span>}
        <ChevronUp className={`status-expand ${expanded ? "expanded" : ""}`} />
      </button>
      {failed && onRetry && <button className="status-retry" onClick={onRetry} title={connection ? "重新连接 Gateway" : "重新加载桌面配置"}><RefreshCw />重试</button>}
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
          title={`Computer Use · ${computerStatusLabel}\n点击查看 Agent 对电脑的操作`}
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
          aria-label={`${previewIdentity(computerDetail, 0)} Computer Use 详情`}
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
                <span>{computerUse?.capabilities?.environment === "local_vm" ? "虚拟机独立桌面" : `操作时：${computerUseLabel(computerDetail.mode, computerDetail.deliveryPolicy)}`}</span>
                <code>{computerDetail.action}</code>
                {computerDetail.observationKind?.startsWith("ax") && <span>辅助功能 · {computerDetail.axElementCount ?? 0} 个元素</span>}
                <em className={computerDetail.status}>{computerDetail.status === "busy" ? "正在操作" : computerDetail.status === "error" ? "失败" : "等待后续"}</em>
              </div>
              <button
                ref={computerDetailCloseRef}
                type="button"
                aria-label="关闭 Computer Use 详情"
                title="关闭 Computer Use 详情"
                onClick={() => setComputerDetail(null)}
              ><X /></button>
            </header>
            <div className="computer-use-detail-body">
              <div className="computer-use-detail-image">
                {detailFrame ? (
                  <div className="computer-use-detail-frame">
                    <img src={`data:${detailFrame.media_type};base64,${detailFrame.data}`} alt={`${previewIdentity(computerDetail, 0)} Computer Use 完整截图`} />
                    {computerDetail.observationKind === "ax" && <span className="computer-use-frame-age">监看截图{computerDetail.frameUpdatedAt ? ` · ${new Date(computerDetail.frameUpdatedAt).toLocaleTimeString("zh-CN", { hour12: false })}` : ""}；本次模型读取 AX Tree</span>}
                    {detailCursor && detailCursorLeft >= 0 && detailCursorLeft <= 100 && detailCursorTop >= 0 && detailCursorTop <= 100 && (
                      <MousePointer2
                        className="computer-use-cursor"
                        style={{ left: `${detailCursorLeft}%`, top: `${detailCursorTop}%` }}
                        aria-label={`光标 ${detailCursor.x}, ${detailCursor.y}`}
                      />
                    )}
                  </div>
                ) : (
                  <div className="computer-use-empty"><MonitorUp /><span>暂无截图</span></div>
                )}
              </div>
              <aside className="computer-use-detail-sidebar">
                <dl>
                  <div><dt>会话</dt><dd title={computerDetail.sessionId}>{computerDetail.sessionId || "未知"}</dd></div>
                  <div><dt>Agent</dt><dd title={computerDetail.agentId}>{computerDetail.agentId || "主 Agent"}</dd></div>
                  <div><dt>更新时间</dt><dd>{new Date(computerDetail.updatedAt).toLocaleTimeString("zh-CN", { hour12: false })}</dd></div>
                  {detailFrame && <div><dt>截图</dt><dd>{detailFrame.width} × {detailFrame.height} · ({detailFrame.origin_x}, {detailFrame.origin_y})</dd></div>}
                </dl>
                <section>
                  <h3>操作记录</h3>
                  <div className="computer-use-detail-log">
                    {detailLogs.length ? [...detailLogs].reverse().map((entry) => (
                      <div className={entry.ok ? "ok" : "failed"} key={entry.id}>
                        <time>{new Date(entry.time).toLocaleTimeString("zh-CN", { hour12: false })}</time>
                        <code>{entry.action}</code>
                        <span>{entry.summary}</span>
                      </div>
                    )) : <p>暂无操作记录。</p>}
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
  const scope = mode === "background_app" ? "指定窗口" : "整个桌面";
  return `${scope} · ${policy === "allow_foreground" ? "允许前台" : policy === "strict_background" ? "严格后台" : "权限未声明"}`;
}
