import {
  AlertTriangle,
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  Pencil,
  Layers3,
  LoaderCircle,
  Plus,
  RefreshCw,
  Search,
  Server,
  Save,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import type {
  ConnectionPreset,
  GatewayViewState,
  ModelSettingsEntry,
  ModelSettingsMutation,
  ModelSettingsSource,
  ModelSettingsResponse,
  ProjectPreset,
} from "./types";

interface ModelSettingsPanelProps {
  activeConnection: ConnectionPreset | null;
  activeProject: ProjectPreset | null;
  gateway: GatewayViewState | null;
  data: ModelSettingsResponse | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
  onMutate?: (mutation: ModelSettingsMutation) => Promise<void>;
}

interface ModelGroupView {
  name: string | null;
  config: Record<string, unknown>;
  models: ModelSettingsEntry[];
}

const DETAIL_FIELDS: Array<{ key: string; label: string }> = [
  { key: "provider", label: "Provider" },
  { key: "model", label: "模型 ID" },
  { key: "base_url", label: "Base URL" },
  { key: "format", label: "API 格式" },
  { key: "api_key_env", label: "API Key 环境变量" },
  { key: "codex_auth_path", label: "Codex 认证文件" },
  { key: "reasoning_effort", label: "推理强度" },
  { key: "thinking_enabled", label: "Thinking" },
  { key: "thinking_budget", label: "Thinking Budget" },
  { key: "max_tokens", label: "最大输出 Token" },
  { key: "context_window", label: "上下文窗口" },
  { key: "timeout", label: "超时" },
  { key: "max_retries", label: "最大重试" },
];

const EDIT_FIELDS: Array<{
  key: string;
  label: string;
  type?: "number" | "boolean" | "select" | "json";
  options?: string[];
}> = [
  { key: "provider", label: "Provider" },
  { key: "model", label: "模型 ID" },
  { key: "base_url", label: "Base URL" },
  { key: "format", label: "API 格式" },
  { key: "api_key_env", label: "API Key 环境变量" },
  { key: "codex_auth_path", label: "Codex 认证文件" },
  { key: "reasoning_effort", label: "推理强度", type: "select", options: ["none", "minimal", "low", "medium", "high", "xhigh", "max"] },
  { key: "thinking_enabled", label: "Thinking", type: "boolean" },
  { key: "thinking_budget", label: "Thinking Budget", type: "number" },
  { key: "max_tokens", label: "最大输出 Token", type: "number" },
  { key: "context_window", label: "上下文窗口", type: "number" },
  { key: "timeout", label: "超时（秒）", type: "number" },
  { key: "max_retries", label: "最大重试", type: "number" },
  { key: "pass_reasoning_content", label: "传递推理内容", type: "boolean" },
  { key: "anthropic_stream_transport", label: "Anthropic 流传输", type: "select", options: ["auto", "sdk", "httpx"] },
  { key: "prompt_cache_key", label: "Prompt Cache Key" },
  { key: "prompt_cache_retention", label: "Prompt Cache 保留", type: "select", options: ["in_memory", "24h"] },
  { key: "azure_endpoint", label: "Azure Endpoint" },
  { key: "azure_api_version", label: "Azure API 版本" },
  { key: "azure_deployment", label: "Azure Deployment" },
  { key: "http_headers", label: "HTTP Headers（JSON）", type: "json" },
  { key: "extra_body", label: "Extra Body（JSON）", type: "json" },
];

type EditorTarget = {
  kind: "model" | "group";
  originalName?: string;
  name: string;
  config: Record<string, unknown>;
  source?: ModelSettingsSource["id"];
};

function editableSources(data: ModelSettingsResponse | null): ModelSettingsSource[] {
  return data?.editable_sources ?? [];
}

function valueText(value: unknown): string {
  if (typeof value === "boolean") return value ? "开启" : "关闭";
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function compactPath(path: string): string {
  const normalized = path.replace(/\\/g, "/");
  const marker = normalized.lastIndexOf("/.crabcode/");
  return marker >= 0 ? `…${normalized.slice(marker)}` : normalized;
}

function modelSummary(model: ModelSettingsEntry): string {
  const provider = valueText(model.effective.provider);
  const modelId = valueText(model.effective.model);
  if (provider === "—") return modelId;
  if (modelId === "—") return provider;
  return `${provider} / ${modelId}`;
}

function groupSummary(config: Record<string, unknown>): string {
  const values = [config.provider, config.base_url]
    .filter((value) => value !== null && value !== undefined && value !== "")
    .map(valueText);
  if (values.length > 0) return values.join(" · ");
  const fieldCount = Object.keys(config).length;
  return fieldCount > 0 ? `共享 ${fieldCount} 个字段` : "没有共享字段";
}

export function groupModelSettings(
  data: ModelSettingsResponse,
  query = "",
): ModelGroupView[] {
  const groups = new Map<string | null, ModelSettingsEntry[]>();
  groups.set(null, []);
  Object.keys(data.groups).forEach((name) => groups.set(name, []));
  data.models.forEach((model) => {
    if (!groups.has(model.group)) groups.set(model.group, []);
    groups.get(model.group)!.push(model);
  });

  const needle = query.trim().toLocaleLowerCase("zh-CN");
  return Array.from(groups, ([name, models]) => {
    const config = name ? data.groups[name] ?? {} : {};
    const groupMatches = !needle || (name ?? "未分组").toLocaleLowerCase("zh-CN").includes(needle)
      || JSON.stringify(config).toLocaleLowerCase("zh-CN").includes(needle);
    return {
      name,
      config,
      models: groupMatches
        ? models
        : models.filter((model) => (
          `${model.name} ${JSON.stringify(model.configured)} ${JSON.stringify(model.effective)}`
            .toLocaleLowerCase("zh-CN")
            .includes(needle)
        )),
    };
  }).filter((group) => {
    if (!needle) return group.models.length > 0 || group.name !== null;
    const groupMatches = (group.name ?? "未分组").toLocaleLowerCase("zh-CN").includes(needle)
      || JSON.stringify(group.config).toLocaleLowerCase("zh-CN").includes(needle);
    return groupMatches || group.models.length > 0;
  });
}

function DetailOrigin({ model, groupConfig }: {
  model: ModelSettingsEntry;
  groupConfig: Record<string, unknown>;
}) {
  return (
    <div className="model-detail-fields">
      {DETAIL_FIELDS.map(({ key, label }) => {
        const configured = Object.prototype.hasOwnProperty.call(model.configured, key);
        const inherited = !configured && model.group !== null
          && Object.prototype.hasOwnProperty.call(groupConfig, key);
        const hasEffectiveValue = Object.prototype.hasOwnProperty.call(model.effective, key)
          && model.effective[key] !== null
          && model.effective[key] !== "";
        return (
          <div className="model-detail-field" key={key}>
            <span>{label}</span>
            <strong title={valueText(model.effective[key])}>{valueText(model.effective[key])}</strong>
            <small className={configured ? "override" : inherited ? "inherited" : "default"}>
              {configured ? "模型覆盖" : inherited ? `继承 ${model.group}` : hasEffectiveValue ? "默认值" : "未配置"}
            </small>
          </div>
        );
      })}
    </div>
  );
}

function editorValue(value: unknown, type?: string): string | boolean {
  if (type === "boolean") return typeof value === "boolean" ? value : false;
  if (value === null || value === undefined || value === "") return "";
  return type === "json" || typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
}

function ModelSettingsEditor({
  target,
  source,
  sourceLabel,
  cwd,
  groupNames,
  onClose,
  onSave,
}: {
  target: EditorTarget;
  source: ModelSettingsSource["id"];
  sourceLabel: string;
  cwd?: string;
  groupNames: string[];
  onClose: () => void;
  onSave: (mutation: ModelSettingsMutation) => Promise<void>;
}) {
  const [name, setName] = useState(target.name);
  const [values, setValues] = useState<Record<string, string | boolean>>(() => Object.fromEntries(
    EDIT_FIELDS
      .filter(({ key }) => target.kind === "model" || key !== "model")
      .map(({ key, type }) => [key, editorValue(target.config[key], type)]),
  ));
  const [group, setGroup] = useState(
    typeof target.config.group === "string" ? target.config.group : "",
  );
  const [touched, setTouched] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const setValue = (key: string, value: string | boolean) => {
    setValues((current) => ({ ...current, [key]: value }));
    setTouched((current) => new Set(current).add(key));
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const trimmedName = name.trim();
    if (!trimmedName) {
      setError("名称不能为空");
      return;
    }
    const config: Record<string, unknown> = {};
    const removeFields: string[] = [];
    if (target.kind === "model" && group) config.group = group;
    else if (target.kind === "model" && Object.prototype.hasOwnProperty.call(target.config, "group")) removeFields.push("group");

    try {
      for (const field of EDIT_FIELDS) {
        if (target.kind === "group" && field.key === "model") continue;
        const raw = values[field.key];
        const hasOriginal = Object.prototype.hasOwnProperty.call(target.config, field.key);
        const wasTouched = touched.has(field.key);
        // Existing values are kept in the remote file by the partial update;
        // only explicitly changed fields are sent back. This also prevents a
        // redacted secret from ever being written as a literal placeholder.
        if (!wasTouched) continue;
        if (field.type === "boolean") {
          config[field.key] = raw === true;
          continue;
        }
        if (typeof raw !== "string" || !raw.trim()) {
          if (hasOriginal && wasTouched) removeFields.push(field.key);
          continue;
        }
        if (field.type === "number") {
          const parsed = Number(raw);
          if (!Number.isFinite(parsed)) throw new Error(`${field.label}必须是数字`);
          config[field.key] = parsed;
        } else if (field.type === "json") {
          const parsed = JSON.parse(raw);
          if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
            throw new Error(`${field.label}必须是 JSON 对象`);
          }
          config[field.key] = parsed;
        } else {
          config[field.key] = raw.trim();
        }
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      return;
    }

    setSaving(true);
    setError(null);
    try {
      await onSave({
        action: target.kind === "model" ? "upsert_model" : "upsert_group",
        source,
        cwd,
        name: trimmedName,
        previous_name: target.originalName,
        config,
        remove_fields: removeFields,
      });
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="model-editor-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !saving) onClose();
    }}>
      <form className="model-editor" role="dialog" aria-modal="true" aria-label={target.kind === "model" ? "模型编辑器" : "配置组编辑器"} onSubmit={submit}>
        <header>
          <div>
            <strong>{target.originalName ? "编辑" : "新增"}{target.kind === "model" ? "模型" : "配置组"}</strong>
            <small>写入 {sourceLabel}</small>
          </div>
          <button className="icon-button small" type="button" aria-label="关闭编辑器" title="关闭" onClick={onClose} disabled={saving}><X /></button>
        </header>
        <div className="model-editor-body">
          <label className="model-editor-field model-editor-name">
            <span>名称</span>
            <input value={name} onChange={(event) => setName(event.target.value)} autoFocus />
          </label>
          {target.kind === "model" && (
            <label className="model-editor-field">
              <span>配置组</span>
              <select value={group} onChange={(event) => setGroup(event.target.value)}>
                <option value="">不使用配置组</option>
                {groupNames.map((groupName) => <option value={groupName} key={groupName}>{groupName}</option>)}
              </select>
            </label>
          )}
          <div className="model-editor-grid">
            {EDIT_FIELDS.map((field) => {
              if (target.kind === "group" && field.key === "model") return null;
              const value = values[field.key];
              if (field.type === "boolean") {
                return (
                  <label className="model-editor-field model-editor-checkbox" key={field.key}>
                    <input type="checkbox" checked={value === true} onChange={(event) => setValue(field.key, event.target.checked)} />
                    <span>{field.label}</span>
                  </label>
                );
              }
              if (field.type === "select") {
                return (
                  <label className="model-editor-field" key={field.key}>
                    <span>{field.label}</span>
                    <select value={typeof value === "string" ? value : ""} onChange={(event) => setValue(field.key, event.target.value)}>
                      <option value="">未设置</option>
                      {field.options?.map((option) => <option value={option} key={option}>{option}</option>)}
                    </select>
                  </label>
                );
              }
              return (
                <label className={`model-editor-field ${field.type === "json" ? "wide" : ""}`} key={field.key}>
                  <span>{field.label}</span>
                  {field.type === "json" ? (
                    <textarea value={typeof value === "string" ? value : ""} rows={3} onChange={(event) => setValue(field.key, event.target.value)} />
                  ) : (
                    <input type={field.type === "number" ? "number" : "text"} value={typeof value === "string" ? value : ""} onChange={(event) => setValue(field.key, event.target.value)} />
                  )}
                </label>
              );
            })}
          </div>
          {error && <div className="settings-inline-note model-settings-error"><AlertTriangle />{error}</div>}
        </div>
        <footer>
          <button className="settings-command" type="button" onClick={onClose} disabled={saving}>取消</button>
          <button className="settings-command primary" type="submit" disabled={saving}>
            {saving ? <LoaderCircle className="spin" /> : <Save />}
            <span>{saving ? "保存中" : "保存"}</span>
          </button>
        </footer>
      </form>
    </div>
  );
}

export function ModelSettingsPanel({
  activeConnection,
  activeProject,
  gateway,
  data,
  loading,
  error,
  onRefresh,
  onMutate,
}: ModelSettingsPanelProps) {
  const [query, setQuery] = useState("");
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());
  const [editor, setEditor] = useState<EditorTarget | null>(null);
  const [source, setSource] = useState<ModelSettingsSource["id"]>(
    activeProject ? "projectSettings" : "userSettings",
  );
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [mutationBusy, setMutationBusy] = useState(false);
  const grouped = useMemo(() => data ? groupModelSettings(data, query) : [], [data, query]);
  const visibleModels = grouped.flatMap((group) => group.models);
  const sourceOptions = editableSources(data);
  const canEdit = onlineGateway(gateway)
    && Boolean(onMutate)
    && (sourceOptions.length === 0 || sourceOptions.some((item) => item.writable));

  function onlineGateway(view: GatewayViewState | null): boolean {
    return view?.status === "online";
  }

  useEffect(() => {
    if (!data?.models.length) {
      setSelectedName(null);
      return;
    }
    if (!selectedName || !data.models.some((model) => model.name === selectedName)) {
      setSelectedName(data.models.find((model) => model.is_default)?.name ?? data.models[0].name);
    }
  }, [data, selectedName]);

  const selected = data?.models.find((model) => model.name === selectedName) ?? null;
  const selectedGroup = selected?.group ? data?.groups[selected.group] ?? {} : {};
  const online = gateway?.status === "online";

  const sourceForModel = (model: ModelSettingsEntry): ModelSettingsSource["id"] => {
    const matching = sourceOptions.filter((item) => item.writable && model.sources.includes(item.path));
    return matching[matching.length - 1]?.id ?? source;
  };

  useEffect(() => {
    if (sourceOptions.length > 0 && !sourceOptions.some((item) => item.id === source)) {
      setSource(sourceOptions.find((item) => item.writable)?.id ?? sourceOptions[0].id);
    }
  }, [source, sourceOptions]);

  const mutate = async (mutation: ModelSettingsMutation) => {
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

  const removeEntry = async (
    kind: "model" | "group",
    name: string,
    targetSource: ModelSettingsSource["id"] = source,
  ) => {
    if (!onMutate || !window.confirm(`确定删除${kind === "model" ? "模型" : "配置组"}“${name}”吗？`)) return;
    try {
      await mutate({
        action: kind === "model" ? "delete_model" : "delete_group",
        source: targetSource,
        cwd: activeProject?.path,
        name,
      });
      if (kind === "model" && selectedName === name) setSelectedName(null);
    } catch {
      // The mutation banner contains the remote error.
    }
  };

  const setDefaultModel = async (name: string | null) => {
    try {
      await mutate({
        action: name ? "set_default_model" : "clear_default_model",
        source,
        cwd: activeProject?.path,
        ...(name ? { name } : {}),
      });
    } catch {
      // The mutation banner contains the remote error.
    }
  };

  const groupKey = (name: string | null) => name === null ? "__ungrouped__" : `group:${name}`;
  const isGroupCollapsed = (name: string | null) => (
    query.trim().length === 0 && collapsedGroups.has(groupKey(name))
  );
  const toggleGroup = (name: string | null) => {
    const key = groupKey(name);
    setCollapsedGroups((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  return (
    <section className="settings-section model-settings-section" aria-labelledby="model-settings-title">
      <div className="settings-section-heading">
        <div>
          <h2 id="model-settings-title">模型目录</h2>
          <p>查看并编辑当前 Gateway 的命名模型、配置组与默认模型。</p>
        </div>
        <div className="model-settings-heading-actions">
          {canEdit && <button className="settings-command" type="button" disabled={mutationBusy || !data} onClick={() => setEditor({ kind: "group", name: "", config: {} })}><Plus /><span>新增配置组</span></button>}
          {canEdit && <button className="settings-command" type="button" disabled={mutationBusy || !data} onClick={() => setEditor({ kind: "model", name: "", config: {} })}><Plus /><span>新增模型</span></button>}
          <button
            className="settings-command model-refresh-command"
            type="button"
            disabled={!online || loading || mutationBusy}
            onClick={onRefresh}
          >
            <RefreshCw className={loading ? "spin" : ""} />
            <span>{loading ? "读取中" : "刷新"}</span>
          </button>
        </div>
      </div>

      <div className="model-context-bar">
        <span><Server />Gateway</span>
        <strong>{activeConnection?.name ?? "未选择"}</strong>
        <span className="model-context-divider" />
        <span>项目</span>
        <strong title={activeProject?.path}>{activeProject?.name ?? "Gateway 默认目录"}</strong>
        {canEdit && sourceOptions.length > 0 && (
          <label className="model-source-picker">
            <span>写入层</span>
            <select aria-label="保存到配置层" value={source} onChange={(event) => setSource(event.target.value as ModelSettingsSource["id"])}>
              {sourceOptions.filter((item) => item.writable).map((item) => <option value={item.id} key={item.id}>{item.label}</option>)}
            </select>
          </label>
        )}
        {data?.default_model && (
          <><span className="model-context-divider" /><span>默认模型</span><strong>{data.default_model}</strong></>
        )}
      </div>

      {!online && (
        <div className="settings-inline-note"><AlertTriangle />连接 Gateway 后才能读取模型配置。</div>
      )}
      {error && (
        <div className="settings-inline-note model-settings-error"><AlertTriangle />{error}</div>
      )}
      {mutationError && mutationError !== error && (
        <div className="settings-inline-note model-settings-error"><AlertTriangle />{mutationError}</div>
      )}
      {data?.warnings.map((warning) => (
        <div className="settings-inline-note" key={warning}><AlertTriangle />{warning}</div>
      ))}

      {online && loading && !data && (
        <div className="model-settings-loading"><LoaderCircle className="spin" />正在读取模型配置</div>
      )}

      {online && !loading && !error && data && data.models.length === 0 && (
        <div className="model-settings-empty">
          <Bot />
          <strong>还没有命名模型</strong>
          <span>在当前 Gateway 的配置层中添加第一个模型。</span>
          {canEdit && <button className="settings-command primary" type="button" onClick={() => setEditor({ kind: "model", name: "", config: {} })}><Plus /><span>新增模型</span></button>}
        </div>
      )}

      {data && data.models.length > 0 && (
        <>
          <label className="model-settings-search">
            <Search />
            <input
              aria-label="搜索模型配置"
              value={query}
              placeholder="搜索模型、Provider 或配置组"
              onChange={(event) => setQuery(event.target.value)}
            />
            <span>{visibleModels.length}/{data.models.length}</span>
          </label>

          <div className="model-settings-browser">
            <div className="model-settings-groups" aria-label="模型列表">
              {grouped.map((group) => (
                <section
                  className={`model-settings-group ${isGroupCollapsed(group.name) ? "is-collapsed" : ""}`}
                  id={`model-group-${groupKey(group.name)}`}
                  key={groupKey(group.name)}
                >
                  <header>
                    <div className="model-group-header-row">
                      <button
                        className="model-settings-group-toggle"
                        type="button"
                        aria-controls={`model-group-${groupKey(group.name)}`}
                        aria-expanded={!isGroupCollapsed(group.name)}
                        onClick={() => toggleGroup(group.name)}
                      >
                        <span className="model-group-icon"><Layers3 /></span>
                        <span>
                          <strong>{group.name ?? "未分组"}</strong>
                          <small title={groupSummary(group.config)}>{groupSummary(group.config)}</small>
                        </span>
                        <em>{group.models.length}</em>
                        <ChevronDown aria-hidden="true" />
                      </button>
                      {canEdit && group.name !== null && (
                        <span className="model-group-actions">
                          <button className="icon-button small" type="button" aria-label={`编辑配置组 ${group.name}`} title="编辑配置组" disabled={mutationBusy} onClick={() => setEditor({ kind: "group", name: group.name!, originalName: group.name!, config: group.config, source })}><Pencil /></button>
                          <button className="icon-button small danger-icon-button" type="button" aria-label={`删除配置组 ${group.name}`} title="删除配置组" disabled={mutationBusy} onClick={() => void removeEntry("group", group.name!)}><Trash2 /></button>
                        </span>
                      )}
                    </div>
                  </header>
                  {group.models.map((model) => (
                    <button
                      className={selected?.name === model.name ? "active" : ""}
                      type="button"
                      key={model.name}
                      aria-label={`查看模型 ${model.name}`}
                      onClick={() => setSelectedName(model.name)}
                    >
                      <span>
                        <strong>{model.name}</strong>
                        <small title={modelSummary(model)}>{modelSummary(model)}</small>
                      </span>
                      {model.is_default && <em><Check />默认</em>}
                      <ChevronRight />
                    </button>
                  ))}
                </section>
              ))}
              {grouped.length === 0 && <div className="settings-list-empty">没有匹配的模型</div>}
            </div>

            <div className="model-settings-detail">
              {selected ? (
                <>
                  <header>
                    <span className="model-detail-icon"><Bot /></span>
                    <span>
                      <strong>{selected.name}</strong>
                      <small>{modelSummary(selected)}</small>
                    </span>
                    {selected.is_default && <em><Check />默认模型</em>}
                    {canEdit && <span className="model-detail-actions">
                      <button className="icon-button small" type="button" aria-label={`编辑模型 ${selected.name}`} title="编辑模型" disabled={mutationBusy} onClick={() => setEditor({ kind: "model", name: selected.name, originalName: selected.name, config: selected.configured, source: sourceForModel(selected) })}><Pencil /></button>
                      <button className="icon-button small danger-icon-button" type="button" aria-label={`删除模型 ${selected.name}`} title="删除模型" disabled={mutationBusy} onClick={() => void removeEntry("model", selected.name, sourceForModel(selected))}><Trash2 /></button>
                    </span>}
                  </header>

                  {canEdit && <div className="model-detail-commands">
                    {selected.is_default
                      ? <button className="settings-command" type="button" disabled={mutationBusy} onClick={() => void setDefaultModel(null)}><X /><span>取消默认模型</span></button>
                      : <button className="settings-command" type="button" disabled={mutationBusy} onClick={() => void setDefaultModel(selected.name)}><Check /><span>设为默认模型</span></button>}
                  </div>}

                  <div className="model-detail-meta">
                    <div><span>配置组</span><strong>{selected.group ?? "未分组"}</strong></div>
                    <div>
                      <span>显式覆盖</span>
                      <strong>{selected.overridden_fields.length ? selected.overridden_fields.join(", ") : "无"}</strong>
                    </div>
                  </div>

                  <DetailOrigin model={selected} groupConfig={selectedGroup} />

                  <details className="model-config-json">
                    <summary>配置文件中的原始字段</summary>
                    <pre>{JSON.stringify(selected.configured, null, 2)}</pre>
                  </details>
                  <details className="model-config-json">
                    <summary>最终生效配置</summary>
                    <pre>{JSON.stringify(selected.effective, null, 2)}</pre>
                  </details>

                  {selected.sources.length > 0 && (
                    <div className="model-detail-sources">
                      <span>配置来源</span>
                      {selected.sources.map((source) => <code title={source} key={source}>{compactPath(source)}</code>)}
                    </div>
                  )}
                </>
              ) : (
                <div className="settings-list-empty">选择一个模型查看详情</div>
              )}
            </div>
          </div>

          {data.sources.length > 0 && (
            <div className="model-settings-sources">
              <span>已读取 {data.sources.length} 个配置层</span>
              <code title={data.cwd}>{data.cwd}</code>
            </div>
          )}
        </>
      )}
      {editor && data && (
        <ModelSettingsEditor
          key={`${editor.kind}:${editor.originalName ?? "new"}`}
          target={editor}
          source={editor.source ?? source}
          sourceLabel={sourceOptions.find((item) => item.id === (editor.source ?? source))?.label ?? (editor.source ?? source)}
          cwd={activeProject?.path}
          groupNames={Object.keys(data.groups)}
          onClose={() => setEditor(null)}
          onSave={mutate}
        />
      )}
    </section>
  );
}
