import { useEffect, useRef, useState } from "react";
import { Download, LoaderCircle, RefreshCw, X } from "lucide-react";
import { t as tr } from "./i18n";
import type { DiscoveredModelSettings, ModelDiscoveryResponse } from "./types";

export function GroupModelDiscovery({ group, sourceLabel, existingIds, onDiscover, onImport, onClose }: {
  group: string;
  sourceLabel: string;
  existingIds: Set<string>;
  onDiscover: (group: string) => Promise<ModelDiscoveryResponse>;
  onImport: (ids: string[], metadata: Record<string, DiscoveredModelSettings>) => Promise<void>;
  onClose: () => void;
}) {
  const [models, setModels] = useState<ModelDiscoveryResponse["models"] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const discoverRef = useRef(onDiscover);
  discoverRef.current = onDiscover;
  const refresh = async () => {
    const current = ++generation.current;
    setLoading(true);
    setError(null);
    setModels(null);
    setSelected(new Set());
    try {
      const result = await discoverRef.current(group);
      if (current === generation.current) setModels(result.models);
    } catch (reason) {
      if (current === generation.current) setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (current === generation.current) setLoading(false);
    }
  };
  useEffect(() => {
    void refresh();
    return () => { generation.current += 1; };
  }, [group]);
  const visible = (models ?? []).filter(model => `${model.id} ${model.name ?? ""}`.toLowerCase().includes(query.trim().toLowerCase()));
  const available = visible.filter(model => !existingIds.has(model.id));
  const selectedIds = [...selected].filter(id => !existingIds.has(id));
  const importModels = async () => {
    setSaving(true);
    setError(null);
    try {
      const metadata = Object.fromEntries((models ?? [])
        .filter(model => selectedIds.includes(model.id) && model.config)
        .map(model => [model.id, model.config!]));
      await onImport(selectedIds, metadata);
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(false);
    }
  };
  return <div className="model-editor-backdrop" onMouseDown={event => {
    if (event.target === event.currentTarget && !saving) onClose();
  }}>
    <section className="model-editor model-discovery" role="dialog" aria-modal="true" aria-label={tr("获取模型列表")} onKeyDown={event => {
      if (event.key === "Escape" && !saving) onClose();
    }}>
      <header>
        <div><strong>{tr("获取模型列表")} · {group}</strong><small>{tr("写入 ")}{sourceLabel}</small></div>
        <button type="button" className="icon-button small" aria-label={tr("关闭")} disabled={saving} onClick={onClose}><X /></button>
      </header>
      <div className="model-editor-body">
        <p className="model-discovery-description">{tr("选择要加入配置组的模型，已有模型会保留原配置。")}</p>
        <p className="model-discovery-description">{tr("导入时自动填写接口提供的上下文、输出和思考设置；未提供的字段沿用现有配置。")}</p>
        <div className="model-discovery-toolbar">
          <input type="search" aria-label={tr("搜索模型")} placeholder={tr("搜索模型")} value={query} onChange={event => setQuery(event.target.value)} disabled={saving} autoFocus />
          <button type="button" className="settings-command" disabled={loading || saving} onClick={() => void refresh()}><RefreshCw />{tr("刷新")}</button>
        </div>
        {loading && <div role="status" className="model-settings-loading"><LoaderCircle className="spin" />{tr("正在获取模型列表")}</div>}
        {error && <div role="alert" className="settings-inline-note model-settings-error">{error}</div>}
        {selectedIds.length > 1000 && <div role="alert" className="settings-inline-note model-settings-error">{tr("每次最多添加 1000 个模型")}</div>}
        {models && <>
          <label className="model-discovery-option">
            <input type="checkbox" disabled={saving || available.length === 0} checked={available.length > 0 && available.every(model => selected.has(model.id))} onChange={event => {
              const checked = event.target.checked;
              setSelected(current => {
                const next = new Set(current);
                available.forEach(model => checked ? next.add(model.id) : next.delete(model.id));
                return next;
              });
            }} />{tr("全选搜索结果")}
            <small>{tr("已选择 {0} 个模型", { 0: selectedIds.length })}</small>
          </label>
          <div className="model-discovery-list">
            {visible.map(model => <label className="model-discovery-option" key={model.id}>
              <input type="checkbox" aria-label={model.id} checked={existingIds.has(model.id) || selected.has(model.id)} disabled={saving || existingIds.has(model.id)} onChange={event => {
                const checked = event.target.checked;
                setSelected(current => {
                  const next = new Set(current);
                  if (checked) next.add(model.id); else next.delete(model.id);
                  return next;
                });
              }} />
              <span><strong>{model.id}</strong>{model.name && model.name !== model.id && <small>{model.name}</small>}
                {model.config && <small>{[
                  model.config.context_window != null && `${tr("上下文窗口")}: ${model.config.context_window}`,
                  model.config.max_tokens != null && `${tr("最大输出 Token")}: ${model.config.max_tokens}`,
                  model.config.thinking_enabled != null && `${tr("思考")}: ${model.config.thinking_enabled ? tr("开启") : tr("关闭")}`,
                ].filter(Boolean).join(" · ")}</small>}
              </span>
              {existingIds.has(model.id) && <small>{tr("已添加")}</small>}
            </label>)}
            {!visible.length && <p className="settings-list-empty">{models.length ? tr("没有匹配的模型") : tr("接口未返回可用模型")}</p>}
          </div>
        </>}
      </div>
      <footer>
        <button type="button" className="settings-command" disabled={saving} onClick={onClose}>{tr("取消")}</button>
        <button type="button" className="settings-command primary" disabled={loading || saving || !selectedIds.length || selectedIds.length > 1000} onClick={() => void importModels()}>
          {saving ? <LoaderCircle className="spin" /> : <Download />}{tr("添加所选模型")}
        </button>
      </footer>
    </section>
  </div>;
}
