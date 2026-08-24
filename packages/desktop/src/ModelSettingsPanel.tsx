import {
  AlertTriangle,
  Bot,
  Check,
  ChevronRight,
  Layers3,
  LoaderCircle,
  RefreshCw,
  Search,
  Server,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type {
  ConnectionPreset,
  GatewayViewState,
  ModelSettingsEntry,
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

export function ModelSettingsPanel({
  activeConnection,
  activeProject,
  gateway,
  data,
  loading,
  error,
  onRefresh,
}: ModelSettingsPanelProps) {
  const [query, setQuery] = useState("");
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const grouped = useMemo(() => data ? groupModelSettings(data, query) : [], [data, query]);
  const visibleModels = grouped.flatMap((group) => group.models);

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

  return (
    <section className="settings-section model-settings-section" aria-labelledby="model-settings-title">
      <div className="settings-section-heading">
        <div>
          <h2 id="model-settings-title">模型目录</h2>
          <p>读取当前 Gateway 的模型配置；配置修改由本地脚手架负责。</p>
        </div>
        <button
          className="settings-command model-refresh-command"
          type="button"
          disabled={!online || loading}
          onClick={onRefresh}
        >
          <RefreshCw className={loading ? "spin" : ""} />
          <span>{loading ? "读取中" : "刷新"}</span>
        </button>
      </div>

      <div className="model-context-bar">
        <span><Server />Gateway</span>
        <strong>{activeConnection?.name ?? "未选择"}</strong>
        <span className="model-context-divider" />
        <span>项目</span>
        <strong title={activeProject?.path}>{activeProject?.name ?? "Gateway 默认目录"}</strong>
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
          <span>请使用本地脚手架生成 models 配置，然后刷新这里。</span>
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
                <section className="model-settings-group" key={group.name ?? "__ungrouped__"}>
                  <header>
                    <span className="model-group-icon"><Layers3 /></span>
                    <span>
                      <strong>{group.name ?? "未分组"}</strong>
                      <small title={groupSummary(group.config)}>{groupSummary(group.config)}</small>
                    </span>
                    <em>{group.models.length}</em>
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
                  </header>

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
    </section>
  );
}
