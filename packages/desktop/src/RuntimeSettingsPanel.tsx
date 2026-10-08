import {
  AlertTriangle,
  HardDrive,
  LoaderCircle,
  MonitorUp,
  Plus,
  RefreshCw,
  Server,
  Trash2,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { isWindowsPlatform } from "./platform";
import type {
  ConnectionPreset,
  GatewayViewState,
  ModelSettingsSource,
  ProjectPreset,
  RuntimeSettingsMutation,
  RuntimeSettingsResponse,
} from "./types";

interface RuntimeSettingsPanelProps {
  localVmSelected?: boolean;
  computerUseEnvironment?: ReactNode;
  activeConnection: ConnectionPreset | null;
  activeProject: ProjectPreset | null;
  gateway: GatewayViewState | null;
  data: RuntimeSettingsResponse | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
  onMutate?: (mutation: RuntimeSettingsMutation) => Promise<void>;
}

function editableSources(data: RuntimeSettingsResponse | null): ModelSettingsSource[] {
  return data?.editable_sources ?? [];
}

function compactPath(path: string): string {
  const normalized = path.replace(/\\/g, "/");
  const marker = normalized.lastIndexOf("/.crabcode/");
  return marker >= 0 ? `…${normalized.slice(marker)}` : normalized;
}

export function RuntimeSettingsPanel({
  localVmSelected = false,
  computerUseEnvironment,
  activeConnection,
  activeProject,
  gateway,
  data,
  loading,
  error,
  onRefresh,
  onMutate,
}: RuntimeSettingsPanelProps) {
  const [source, setSource] = useState<ModelSettingsSource["id"]>(
    activeProject ? "projectSettings" : "userSettings",
  );
  const [snapshotSizeDraft, setSnapshotSizeDraft] = useState("");
  const [compactBufferDraft, setCompactBufferDraft] = useState("");
  const [compactLimitDraft, setCompactLimitDraft] = useState("");
  const [toolPath, setToolPath] = useState("");
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [mutationBusy, setMutationBusy] = useState(false);
  const sourceOptions = editableSources(data);
  const writableSources = useMemo(() => sourceOptions.filter((item) => item.writable), [sourceOptions]);
  const online = gateway?.status === "online";
  const canEdit = online && Boolean(onMutate) && writableSources.length > 0;
  const storedTarget = data?.computer_use_target_scope
    ?? (data?.computer_use_mode === "foreground_desktop" ? "desktop" : "app_window");
  const storedPolicy = data?.computer_use_delivery_policy ?? "allow_foreground";
  const windowsHost = isWindowsPlatform();
  // The shared default is app_window for macOS. On Windows that choice cannot
  // run, so the control shows and keeps the desktop scope instead.
  const computerUseTarget = windowsHost && storedTarget === "app_window" ? "desktop" : storedTarget;
  const computerUsePolicy = windowsHost && storedPolicy === "strict_background" ? "allow_foreground" : storedPolicy;
  const windowsCorrection = useRef(false);

  useEffect(() => {
    setSnapshotSizeDraft(data ? String(data.snapshot_max_size_mb) : "");
  }, [data?.snapshot_max_size_mb]);

  useEffect(() => {
    setCompactBufferDraft(String(data?.compact_buffer_tokens ?? 20_000));
    setCompactLimitDraft(data?.max_context_length == null ? "" : String(data.max_context_length));
  }, [data?.cwd, data?.compact_buffer_tokens, data?.max_context_length]);

  useEffect(() => {
    if (writableSources.length > 0 && !writableSources.some((item) => item.id === source)) {
      setSource(writableSources.find((item) => item.id === "projectSettings")?.id ?? writableSources[0].id);
    }
  }, [source, writableSources]);

  const mutate = async (mutation: RuntimeSettingsMutation) => {
    if (!onMutate) return;
    setMutationBusy(true);
    setMutationError(null);
    try {
      await onMutate(mutation);
    } catch (reason) {
      setMutationError(reason instanceof Error ? reason.message : String(reason));
      throw reason;
    } finally {
      setMutationBusy(false);
    }
  };

  const saveCompaction = async (
    changes: Pick<RuntimeSettingsMutation, "auto_compact_enabled" | "compact_buffer_tokens" | "max_context_length">,
  ) => {
    try {
      await mutate({ action: "set_compaction", source, cwd: activeProject?.path, ...changes });
    } catch {
      // Keep the draft available for retry; the banner shows the error.
    }
  };

  const saveCompactionTokens = (field: "compact_buffer_tokens" | "max_context_length", draft: string) => {
    const value = draft.trim();
    const next = field === "max_context_length" && value === "" ? null : Number(value);
    if (next !== null && (value === "" || !Number.isSafeInteger(next) || next < (field === "compact_buffer_tokens" ? 0 : 1))) {
      setMutationError(field === "compact_buffer_tokens" ? "压缩预留 token 必须是非负整数" : "提前触发阈值必须是正整数，或留空使用自动阈值");
      return;
    }
    const current = field === "compact_buffer_tokens" ? data?.compact_buffer_tokens ?? 20_000 : data?.max_context_length ?? null;
    setMutationError(null);
    if (next !== current) void saveCompaction({ [field]: next });
  };

  const saveSnapshot = async (
    changes: Pick<RuntimeSettingsMutation, "snapshot_enabled" | "snapshot_max_size_mb">,
    targetSource = source,
  ) => {
    try {
      await mutate({ action: "set_snapshot", source: targetSource, cwd: activeProject?.path, ...changes });
    } catch {
      // The mutation banner contains the remote error.
    }
  };

  const saveComputerUseOptions = async (
    changes: Pick<RuntimeSettingsMutation, "computer_use_target_scope" | "computer_use_delivery_policy">,
  ) => {
    try {
      await mutate({
        action: "set_computer_use_options",
        source,
        cwd: activeProject?.path,
        ...changes,
      });
    } catch {
      // The mutation banner contains the remote error.
    }
  };

  useEffect(() => {
    if (!windowsHost || !canEdit || !data || windowsCorrection.current) return;
    const changes: Pick<RuntimeSettingsMutation, "computer_use_target_scope" | "computer_use_delivery_policy"> = {};
    if (storedTarget === "app_window") changes.computer_use_target_scope = "desktop";
    if (storedPolicy === "strict_background") changes.computer_use_delivery_policy = "allow_foreground";
    if (!changes.computer_use_target_scope && !changes.computer_use_delivery_policy) return;
    windowsCorrection.current = true;
    void saveComputerUseOptions(changes);
  }, [windowsHost, canEdit, data, storedTarget, storedPolicy, source]);

  const addTool = async (event: FormEvent) => {
    event.preventDefault();
    const value = toolPath.trim();
    if (!value) {
      setMutationError("额外工具路径不能为空");
      return;
    }
    try {
      await mutate({ action: "add_extra_tool", source, cwd: activeProject?.path, tool_path: value });
      setToolPath("");
    } catch {
      // The mutation banner contains the remote error.
    }
  };

  const removeTool = async (tool: string, targetSource: ModelSettingsSource["id"]) => {
    if (!window.confirm(`从当前配置层移除额外工具“${tool}”？`)) return;
    try {
      await mutate({ action: "remove_extra_tool", source: targetSource, cwd: activeProject?.path, tool_path: tool });
    } catch {
      // The mutation banner contains the remote error.
    }
  };

  const sourceForTool = (tool: string): ModelSettingsSource["id"] => {
    const matching = writableSources.filter((item) => data?.extra_tools_by_source[item.id]?.includes(tool));
    return matching[matching.length - 1]?.id ?? source;
  };

  return (
    <section className="settings-section runtime-settings-section" aria-labelledby="runtime-settings-title">
      <div className="settings-section-heading">
        <div>
          <h2 id="runtime-settings-title">运行与工具</h2>
          <p>管理 Gateway 的上下文压缩、Computer Use、文件快照和额外工具配置。</p>
        </div>
        <button
          className="settings-command"
          type="button"
          disabled={!online || loading || mutationBusy}
          onClick={onRefresh}
        >
          <RefreshCw className={loading ? "spin" : ""} />
          <span>{loading ? "读取中" : "刷新"}</span>
        </button>
      </div>

      <div className="runtime-context-bar">
        <span><Server />Gateway</span>
        <strong>{activeConnection?.name ?? "未选择"}</strong>
        <span className="runtime-context-divider" />
        <span>项目</span>
        <strong title={activeProject?.path}>{activeProject?.name ?? "Gateway 默认目录"}</strong>
        {canEdit && (
          <label className="runtime-source-picker">
            <span>写入层</span>
            <select aria-label="运行设置保存到配置层" value={source} onChange={(event) => setSource(event.target.value as ModelSettingsSource["id"])}>
              {writableSources.map((item) => <option value={item.id} key={item.id}>{item.label}</option>)}
            </select>
          </label>
        )}
      </div>

      {!online && <div className="settings-inline-note"><AlertTriangle />连接 Gateway 后才能读取运行设置。</div>}
      {error && <div className="settings-inline-note model-settings-error"><AlertTriangle />{error}</div>}
      {mutationError && mutationError !== error && <div className="settings-inline-note model-settings-error"><AlertTriangle />{mutationError}</div>}
      {data?.warnings.map((warning) => <div className="settings-inline-note" key={warning}><AlertTriangle />{warning}</div>)}
      {online && data && onMutate && writableSources.length === 0 && (
        <div className="settings-inline-note"><AlertTriangle />当前 Gateway 的配置层不可写，设置只能查看。</div>
      )}

      {online && loading && !data && <div className="model-settings-loading"><LoaderCircle className="spin" />正在读取运行设置</div>}

      {online && !loading && !error && data && (
        <section className="runtime-settings-group settings-group" aria-labelledby="compaction-settings-title">
          <div className="settings-subsection-heading">
            <div>
              <h3 id="compaction-settings-title">上下文压缩</h3>
              <p>保存后从下一轮对话生效；正在运行的一轮沿用原设置。</p>
            </div>
          </div>
          <div className="settings-row compact">
            <div className="settings-row-copy"><strong>自动压缩</strong><span>接近上下文上限时，整理历史内容并继续当前任务。</span></div>
            <button className={`settings-switch ${data.auto_compact_enabled !== false ? "on" : ""}`} type="button" role="switch"
              aria-label="自动压缩" aria-checked={data.auto_compact_enabled !== false} disabled={!canEdit || mutationBusy}
              onClick={() => void saveCompaction({ auto_compact_enabled: data.auto_compact_enabled === false })}><span /></button>
          </div>
          <div className="settings-row compact">
            <div className="settings-row-copy"><strong>压缩预留 token</strong><span>默认 20,000。实际至少预留模型的最大输出额度；设为 0 仍保留输出空间。</span></div>
            <input className="settings-number-input" aria-label="压缩预留 token" type="number" min={0} step={1}
              value={compactBufferDraft} disabled={!canEdit || mutationBusy}
              onChange={(event) => setCompactBufferDraft(event.target.value)}
              onBlur={() => saveCompactionTokens("compact_buffer_tokens", compactBufferDraft)}
              onKeyDown={(event) => {
                if (event.key === "Enter") event.currentTarget.blur();
                if (event.key === "Escape") setCompactBufferDraft(String(data.compact_buffer_tokens ?? 20_000));
              }} />
          </div>
          <div className="settings-row compact">
            <div className="settings-row-copy"><strong>提前触发阈值</strong><span>已用 token 超过此值时提前压缩。留空自动计算，设置值不能推迟安全阈值。</span></div>
            <input className="settings-number-input" aria-label="提前触发阈值（tokens）" type="number" min={1} step={1} placeholder="自动"
              value={compactLimitDraft} disabled={!canEdit || mutationBusy}
              onChange={(event) => setCompactLimitDraft(event.target.value)}
              onBlur={() => saveCompactionTokens("max_context_length", compactLimitDraft)}
              onKeyDown={(event) => {
                if (event.key === "Enter") event.currentTarget.blur();
                if (event.key === "Escape") setCompactLimitDraft(data.max_context_length == null ? "" : String(data.max_context_length));
              }} />
          </div>
          <div className="runtime-settings-note">自动阈值 = 上下文容量 − max（压缩预留 token，模型最大输出 token）。</div>
        </section>
      )}

      {online && !loading && !error && data && (
        <section className="runtime-settings-group settings-group" aria-labelledby="snapshot-settings-title">
          <div className="settings-subsection-heading">
            <div>
              <h3 id="snapshot-settings-title">文件快照</h3>
              <p>关闭后仍会保存对话 checkpoint，只跳过工作区文件副本。</p>
            </div>
            <HardDrive aria-hidden="true" />
          </div>
          <div className="settings-row compact">
            <div className="settings-row-copy">
              <strong>启用文件快照</strong>
              <span>创建 checkpoint 或修改文件时，是否记录可供 /revert 恢复的文件快照。</span>
            </div>
            <button
              className={`settings-switch ${data.snapshot_enabled ? "on" : ""}`}
              type="button"
              role="switch"
              aria-checked={data.snapshot_enabled}
              aria-label="启用文件快照"
              disabled={!canEdit || mutationBusy}
              onClick={() => void saveSnapshot({ snapshot_enabled: !data.snapshot_enabled })}
            ><span /></button>
          </div>
          <div className="settings-row compact">
            <div className="settings-row-copy">
              <strong>快照最大大小</strong>
              <span>扫描工作区时的累计上限，单位 MiB，范围 1–1,048,576。</span>
            </div>
            <input
              className="settings-number-input"
              aria-label="快照最大大小（MiB）"
              type="number"
              min={1}
              max={1_048_576}
              step={1}
              value={snapshotSizeDraft}
              disabled={!canEdit || mutationBusy}
              onChange={(event) => setSnapshotSizeDraft(event.target.value)}
              onBlur={() => {
                const next = Number(snapshotSizeDraft);
                if (!Number.isFinite(next)) {
                  setSnapshotSizeDraft(String(data.snapshot_max_size_mb));
                  return;
                }
                const normalized = Math.min(1_048_576, Math.max(1, Math.round(next)));
                setSnapshotSizeDraft(String(normalized));
                if (normalized !== data.snapshot_max_size_mb) void saveSnapshot({ snapshot_max_size_mb: normalized });
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter") event.currentTarget.blur();
                if (event.key === "Escape") setSnapshotSizeDraft(String(data.snapshot_max_size_mb));
              }}
            />
          </div>
        </section>
      )}

      {(computerUseEnvironment || (online && !loading && !error && data)) && (
        <section className="runtime-settings-group settings-group" aria-labelledby="computer-use-settings-title">
          <div className="settings-subsection-heading">
            <div>
              <h3 id="computer-use-settings-title">Computer Use</h3>
              <p>选择 Agent 操作桌面应用的方式；修改对新会话生效。</p>
            </div>
            <MonitorUp aria-hidden="true" />
          </div>
          {computerUseEnvironment}
          {!localVmSelected && online && !loading && !error && data && <>
            <div className="settings-row compact">
              <div className="settings-row-copy">
                <strong>操作目标</strong>
                <span>
                  指定窗口使用窗口内坐标。整个桌面会把所有显示器拼成一张图，点击以这张图的左上角为原点，并控制真实鼠标和键盘。
                </span>
              </div>
              <div className="settings-segmented" aria-label="Computer Use 操作目标">
                {(["app_window", "desktop"] as const).map((scope) => (
                  <button
                    key={scope}
                    type="button"
                    className={computerUseTarget === scope ? "active" : ""}
                    aria-pressed={computerUseTarget === scope}
                    title={windowsHost && scope === "app_window" ? "Windows 暂不支持指定窗口" : undefined}
                    disabled={!canEdit || mutationBusy || (windowsHost && scope === "app_window") || (scope === "desktop" && computerUsePolicy !== "allow_foreground")}
                    onClick={() => {
                      if (windowsHost && scope === "app_window") return;
                      void saveComputerUseOptions({ computer_use_target_scope: scope });
                    }}
                  >
                    {scope === "app_window" ? "指定窗口" : "整个桌面"}
                  </button>
                ))}
              </div>
            </div>
            <div className="settings-row compact">
              <div className="settings-row-copy">
                <strong>前台权限</strong>
                <span>严格后台仅允许已验证的系统、应用版本和输入操作；不支持时返回原因，不会自动切换到前台。允许前台操作可能激活目标窗口。</span>
              </div>
              <div className="settings-segmented" aria-label="Computer Use 前台权限">
                {(["strict_background", "allow_foreground"] as const).map((policy) => (
                  <button
                    key={policy}
                    type="button"
                    className={computerUsePolicy === policy ? "active" : ""}
                    aria-pressed={computerUsePolicy === policy}
                    title={windowsHost && policy === "strict_background" ? "Windows 暂不支持严格后台" : undefined}
                    disabled={!canEdit || mutationBusy || (windowsHost && policy === "strict_background")}
                    onClick={() => {
                      if (windowsHost && policy === "strict_background") return;
                      void saveComputerUseOptions({
                        computer_use_delivery_policy: policy,
                        ...(policy === "strict_background" ? { computer_use_target_scope: "app_window" as const } : {}),
                      });
                    }}
                  >
                    {policy === "strict_background" ? "严格后台" : "允许前台操作"}
                  </button>
                ))}
              </div>
            </div>
            <div className="runtime-settings-note">
              指定窗口目前仅支持 macOS。严格后台不提供聚焦窗口操作，会拒绝未通过验证的输入；允许前台操作可能打断当前操作，并不表示所有输入都支持自动回退。是否允许前台操作仅由此处设置决定，与会话的“完全访问”权限无关。使用整个桌面前仍需选择允许前台操作。
            </div>
          </>}
        </section>
      )}

      {online && !loading && !error && data && (
        <>
          <section className="runtime-settings-group settings-group" aria-labelledby="extra-tools-settings-title">
            <div className="settings-subsection-heading">
              <div>
                <h3 id="extra-tools-settings-title">额外工具</h3>
                <p>使用 Gateway 主机上的 Python import path 挂载自定义 Tool。修改对新会话生效。</p>
              </div>
              <span className="runtime-tool-count">{data.extra_tools.length} 项</span>
            </div>
            <form className="runtime-tool-form" onSubmit={(event) => void addTool(event)}>
              <input
                aria-label="额外工具导入路径"
                placeholder="例如 crabcode_search.CodebaseSearchTool"
                value={toolPath}
                disabled={!canEdit || mutationBusy}
                onChange={(event) => setToolPath(event.target.value)}
              />
              <button className="settings-command primary" type="submit" disabled={!canEdit || mutationBusy || !toolPath.trim()}>
                {mutationBusy ? <LoaderCircle className="spin" /> : <Plus />}
                <span>添加工具</span>
              </button>
            </form>
            {data.extra_tools.length > 0 ? (
              <div className="runtime-tool-list" aria-label="额外工具列表">
                {data.extra_tools.map((tool) => {
                  const toolSource = sourceForTool(tool);
                  const sourceLabel = sourceOptions.find((item) => item.id === toolSource)?.label ?? toolSource;
                  return (
                    <div className="runtime-tool-row" key={tool}>
                      <code title={tool}>{tool}</code>
                      <small>{sourceLabel}</small>
                      {canEdit && (
                        <button
                          className="icon-button small danger-icon-button"
                          type="button"
                          aria-label={`移除额外工具 ${tool}`}
                          title={`从${sourceLabel}移除`}
                          disabled={mutationBusy}
                          onClick={() => {
                            setSource(toolSource);
                            void removeTool(tool, toolSource);
                          }}
                        ><Trash2 /></button>
                      )}
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="runtime-tool-empty">尚未配置额外工具</div>
            )}
            <div className="runtime-settings-note">工具包必须安装在远程 Gateway 环境中；现有会话不会热加载配置。</div>
          </section>

          {data.sources.length > 0 && (
            <div className="runtime-settings-sources">
              <span>已读取 {data.sources.length} 个配置层</span>
              <code title={data.cwd}>{compactPath(data.cwd)}</code>
            </div>
          )}
        </>
      )}
    </section>
  );
}
