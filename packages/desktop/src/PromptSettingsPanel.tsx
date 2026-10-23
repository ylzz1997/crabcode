import { configurationSourceLabel, promptSectionLabel } from "./i18n";
import { getLocale } from "./i18n";
import { t as tr } from "./i18n";
import { AlertTriangle, Download, LoaderCircle, Plus, RefreshCw, Search, Trash2, Upload } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { savePromptExport } from "./native";
import {
  parsePromptTemplateFile,
  promptTemplateFilename,
  serializePromptTemplate,
  serializePromptTemplates,
  type PortablePromptTemplate,
} from "./promptTemplateFile";
import type {
  ConnectionPreset,
  GatewayViewState,
  ModelSettingsSource,
  ProjectPreset,
  PromptSettingsMutation,
  PromptSettingsResponse,
  PromptTemplateView,
} from "./types";

interface PromptSettingsPanelProps {
  activeConnection: ConnectionPreset | null;
  activeProject: ProjectPreset | null;
  gateway: GatewayViewState | null;
  data: PromptSettingsResponse | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
  onMutate?: (mutation: PromptSettingsMutation) => Promise<void>;
}

const DEFAULT_TEMPLATE = "default";

function matchesQuery(value: string, query: string): boolean {
  const needle = query.trim().toLocaleLowerCase(getLocale());
  if (!needle) return true;
  return value.toLocaleLowerCase(getLocale()).includes(needle);
}

function settingsSource(id: string, fallback: ModelSettingsSource["id"]): ModelSettingsSource["id"] {
  if (id === "userSettings" || id === "projectSettings" || id === "localSettings") return id;
  return fallback;
}

function editableSources(data: PromptSettingsResponse | null): ModelSettingsSource[] {
  return (data?.editable_sources ?? []).map(source => ({ ...source, label: configurationSourceLabel(source) }));
}

function draftFrom(template: PromptTemplateView | undefined, keys: string[]): Record<string, string> {
  const draft: Record<string, string> = {};
  for (const key of keys) draft[key] = template?.sections[key] ?? "";
  return draft;
}

function sameDraft(left: Record<string, string>, right: Record<string, string>, keys: string[]): boolean {
  return keys.every((key) => (left[key] ?? "") === (right[key] ?? ""));
}

function sectionsForExport(draft: Record<string, string>, keys: string[]): Record<string, string> {
  const sections: Record<string, string> = {};
  const ordered = [...keys, ...Object.keys(draft).filter((key) => !keys.includes(key))];
  for (const key of ordered) {
    const text = (draft[key] ?? "").trim();
    if (text) sections[key] = text;
  }
  return sections;
}

function readFileText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(typeof reader.result === "string" ? reader.result : "");
    reader.onerror = () => reject(reader.error ?? new Error(tr("无法读取 JSON 文件")));
    reader.readAsText(file);
  });
}

async function downloadPromptFile(filename: string, text: string): Promise<string | null> {
  const bytes = new TextEncoder().encode(text);
  const nativePath = await savePromptExport(filename, bytes);
  if (nativePath) return nativePath;
  const blob = new Blob([bytes], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
  return null;
}

export function PromptSettingsPanel({
  activeConnection,
  activeProject,
  gateway,
  data,
  loading,
  error,
  onRefresh,
  onMutate,
}: PromptSettingsPanelProps) {
  const [source, setSource] = useState<ModelSettingsSource["id"]>(
    activeProject ? "projectSettings" : "userSettings",
  );
  const [selectedId, setSelectedId] = useState(DEFAULT_TEMPLATE);
  const [templateName, setTemplateName] = useState("");
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [promptText, setPromptText] = useState("");
  const [templateQuery, setTemplateQuery] = useState("");
  const [promptQuery, setPromptQuery] = useState("");
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [transferMessage, setTransferMessage] = useState<string | null>(null);
  const [mutationBusy, setMutationBusy] = useState(false);
  const importInputRef = useRef<HTMLInputElement>(null);
  const sourceOptions = editableSources(data);
  const writableSources = useMemo(() => sourceOptions.filter((item) => item.writable), [sourceOptions, getLocale()]);
  const online = gateway?.status === "online";
  const canEdit = online && Boolean(onMutate) && writableSources.length > 0;
  const sectionKeys = useMemo(() => (data?.sections ?? []).map((item) => item.key), [data?.sections, getLocale()]);
  const templateSignature = useMemo(() => JSON.stringify({
    active: data?.active_template_id ?? null,
    sections: sectionKeys,
    templates: (data?.templates ?? []).map((item) => ({
      id: item.id,
      name: item.name,
      sections: item.sections,
    })),
  }), [data?.active_template_id, data?.templates, sectionKeys, getLocale()]);
  const selected = data?.templates.find((item) => item.id === selectedId);
  const savedDraft = draftFrom(selected, sectionKeys);
  const dirty = selectedId === DEFAULT_TEMPLATE
    ? templateName.trim() !== "" || sectionKeys.some((key) => (draft[key] ?? "").trim() !== "")
    : templateName !== (selected?.name ?? "") || !sameDraft(draft, savedDraft, sectionKeys);

  useEffect(() => {
    if (!data) return;
    const id = data.active_template_id || DEFAULT_TEMPLATE;
    const template = data.templates.find((item) => item.id === id);
    const keys = data.sections.map((item) => item.key);
    setSelectedId(id);
    setTemplateName(template?.name ?? "");
    setDraft(draftFrom(template, keys));
  }, [templateSignature]);

  useEffect(() => {
    if (writableSources.length > 0 && !writableSources.some((item) => item.id === source)) {
      setSource(writableSources.find((item) => item.id === "projectSettings")?.id ?? writableSources[0].id);
    }
  }, [source, writableSources]);

  const mutate = async (mutation: PromptSettingsMutation) => {
    if (!onMutate) return;
    setMutationBusy(true);
    setMutationError(null);
    setTransferMessage(null);
    try {
      await onMutate({ ...mutation, cwd: activeProject?.path });
    } catch (reason) {
      setMutationError(reason instanceof Error ? reason.message : String(reason));
      throw reason;
    } finally {
      setMutationBusy(false);
    }
  };

  const confirmDiscard = () => {
    if (!dirty) return true;
    return window.confirm(tr("当前修改尚未保存为模版，切换后会丢弃这些修改。"));
  };

  const selectTemplate = async (id: string) => {
    if (id === selectedId) return;
    if (!confirmDiscard()) return;
    const previousId = selectedId;
    const previousName = templateName;
    const previousDraft = draft;
    const template = data?.templates.find((item) => item.id === id);
    setSelectedId(id);
    setTemplateName(template?.name ?? "");
    setDraft(draftFrom(template, sectionKeys));
    if (!canEdit) return;
    try {
      await mutate({
        action: "set_active_template",
        source,
        template_id: id === DEFAULT_TEMPLATE ? null : id,
      });
    } catch {
      setSelectedId(previousId);
      setTemplateName(previousName);
      setDraft(previousDraft);
    }
  };

  const saveTemplate = async () => {
    const name = templateName.trim();
    if (!name) {
      setMutationError(tr("请先填写模版名称"));
      return;
    }
    try {
      await mutate({
        action: "save_template",
        source,
        template_id: selectedId === DEFAULT_TEMPLATE ? undefined : selectedId,
        template_name: name,
        sections: draft,
      });
    } catch {
      // The mutation banner contains the remote error.
    }
  };

  const deleteTemplate = async (template: PromptTemplateView) => {
    try {
      await mutate({
        action: "delete_template",
        source: settingsSource(template.source, source),
        template_id: template.id,
      });
    } catch {
      // The mutation banner contains the remote error.
    }
  };

  const templatesForExport = (): PortablePromptTemplate[] => {
    const saved: PortablePromptTemplate[] = (data?.templates ?? []).map((item) => (
      item.id === selectedId
        ? {
          id: item.id,
          name: templateName.trim() || item.name,
          sections: sectionsForExport(draft, sectionKeys),
        }
        : { id: item.id, name: item.name, sections: item.sections }
    ));
    if (selectedId === DEFAULT_TEMPLATE && templateName.trim()) {
      saved.push({
        name: templateName.trim(),
        sections: sectionsForExport(draft, sectionKeys),
      });
    }
    return saved;
  };

  const exportTemplates = async (kind: "current" | "all") => {
    setTransferMessage(null);
    const currentName = templateName.trim();
    if (kind === "current" && !currentName) {
      setMutationError(tr("请先填写模版名称"));
      return;
    }
    const templates = kind === "current"
      ? [{
        id: selectedId === DEFAULT_TEMPLATE ? undefined : selectedId,
        name: currentName,
        sections: sectionsForExport(draft, sectionKeys),
      }]
      : templatesForExport();
    if (templates.length === 0) {
      setMutationError(tr("还没有可导出的模版"));
      return;
    }
    try {
      const parsed = parsePromptTemplateFile(JSON.stringify(kind === "all" ? { templates } : templates[0]));
      const text = kind === "all" ? serializePromptTemplates(parsed) : serializePromptTemplate(parsed[0]);
      const filename = kind === "all" ? "prompt-templates.json" : promptTemplateFilename(parsed[0].name);
      const path = await downloadPromptFile(filename, text);
      setMutationError(null);
      setTransferMessage(path
        ? tr("已导出到 {0}", { 0: path })
        : kind === "all" ? tr("已导出 {0} 个模版", { 0: parsed.length }) : tr("已导出 {0}", { 0: parsed[0].name }));
    } catch (reason) {
      setMutationError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const importTemplateFile = async (file: File) => {
    if (!canEdit) return;
    if (dirty && !window.confirm(tr("当前修改尚未保存为模版，导入后会丢弃这些修改。"))) return;
    setTransferMessage(null);
    if (file.size > 12 * 1024 * 1024) {
      setMutationError(tr("JSON 文件过大"));
      return;
    }
    let templates: PortablePromptTemplate[];
    try {
      templates = parsePromptTemplateFile(await readFileText(file));
    } catch (reason) {
      setMutationError(reason instanceof Error ? reason.message : String(reason));
      return;
    }
    let imported = 0;
    try {
      for (const template of templates) {
        await mutate({
          action: "save_template",
          source,
          template_id: template.id,
          template_name: template.name,
          sections: template.sections,
        });
        imported += 1;
      }
      setTransferMessage(imported === 1 ? tr("已导入 {0}", { 0: templates[0].name }) : tr("已导入 {0} 个模版", { 0: imported }));
    } catch (reason) {
      if (imported > 0) {
        const detail = reason instanceof Error ? reason.message : String(reason);
        setMutationError(tr("已导入 {0} 个，随后失败：{1}", { 0: imported, 1: detail }));
      }
    }
  };

  const addPrompt = async (event: FormEvent) => {
    event.preventDefault();
    const text = promptText.trim();
    if (!text) {
      setMutationError(tr("用户提示不能为空"));
      return;
    }
    try {
      await mutate({ action: "add_user_prompt", source, prompt_text: text });
      setPromptText("");
    } catch {
      // The mutation banner contains the remote error.
    }
  };

  const togglePrompt = async (id: string, promptSource: string, enabled: boolean) => {
    try {
      await mutate({
        action: "set_user_prompt_enabled",
        source: settingsSource(promptSource, source),
        prompt_id: id,
        enabled,
      });
    } catch {
      // The mutation banner contains the remote error.
    }
  };

  const deletePrompt = async (id: string, promptSource: string) => {
    try {
      await mutate({
        action: "delete_user_prompt",
        source: settingsSource(promptSource, source),
        prompt_id: id,
      });
    } catch {
      // The mutation banner contains the remote error.
    }
  };

  const sourceLabel = (id: string) => sourceOptions.find((item) => item.id === id)?.label ?? "";
  const creating = selectedId === DEFAULT_TEMPLATE;
  const visibleTemplates = (data?.templates ?? []).filter((item) => matchesQuery(item.name, templateQuery));
  const showDefaultTemplate = matchesQuery(tr("默认"), templateQuery);
  const visiblePrompts = (data?.user_prompts ?? []).filter((item) => matchesQuery(item.text, promptQuery));
  const defaultActive = !data?.active_template_id;

  return (
    <section className="settings-section prompt-settings-section" aria-labelledby="prompt-settings-title">
      <div className="settings-section-heading">
        <div>
          <h2 id="prompt-settings-title">{tr("提示词")}</h2>
          <p>{tr("自定义系统与上下文压缩提示词模版，并选择要追加到用户输入的提示。")}</p>
        </div>
        <button
          className="settings-command"
          type="button"
          disabled={!online || loading || mutationBusy}
          onClick={onRefresh}
        >
          <RefreshCw className={loading ? "spin" : ""} />
          <span>{loading ? tr("读取中") : tr("刷新")}</span>
        </button>
      </div>

      <div className="runtime-context-bar">
        <span>Gateway</span>
        <strong>{activeConnection?.name ?? tr("未选择")}</strong>
        <span className="runtime-context-divider" />
        <span>{tr("项目")}</span>
        <strong title={activeProject?.path}>{activeProject?.name ?? tr("Gateway 默认目录")}</strong>
        {canEdit && (
          <label className="runtime-source-picker">
            <span>{tr("写入层")}</span>
            <select
              aria-label={tr("提示词设置保存到配置层")}
              value={source}
              onChange={(event) => setSource(event.target.value as ModelSettingsSource["id"])}
            >
              {writableSources.map((item) => <option value={item.id} key={item.id}>{item.label}</option>)}
            </select>
          </label>
        )}
      </div>

      {!online && <div className="settings-inline-note"><AlertTriangle />{tr("连接 Gateway 后才能读取提示词设置。")}</div>}
      {error && <div className="settings-inline-note model-settings-error"><AlertTriangle />{error}</div>}
      {mutationError && mutationError !== error && (
        <div className="settings-inline-note model-settings-error"><AlertTriangle />{mutationError}</div>
      )}
      {transferMessage && (
        <div className="settings-inline-note prompt-transfer-note">{transferMessage}</div>
      )}
      {data?.warnings.map((warning) => (
        <div className="settings-inline-note" key={warning}><AlertTriangle />{warning}</div>
      ))}
      {online && loading && !data && (
        <div className="model-settings-loading"><LoaderCircle className="spin" />{tr("正在读取提示词设置")}</div>
      )}

      {online && data && (
        <>
          <section className="runtime-settings-group settings-group prompt-template-card" aria-labelledby="prompt-template-title">
            <div className="settings-subsection-heading">
              <div>
                <h3 id="prompt-template-title">{tr("自定义提示词模版")}</h3>
                <p>{tr("留空的段落使用内置默认。选择「默认」时不套用模版；若配置里已有 prompt_profile，仍会沿用它。导入和导出走 JSON，写入当前选择的配置层。")}</p>
              </div>
            </div>
            <div className="prompt-template-layout">
              <div className="prompt-list-pane">
                <label className="prompt-list-search">
                  <Search />
                  <input
                    aria-label={tr("搜索提示词模版")}
                    value={templateQuery}
                    placeholder={tr("搜索模版")}
                    onChange={(event) => setTemplateQuery(event.target.value)}
                  />
                </label>
                <div className="prompt-list-view" role="listbox" aria-label={tr("使用的提示词模版")}>
                  {showDefaultTemplate && (
                    <div className={`prompt-list-item${selectedId === DEFAULT_TEMPLATE ? " selected" : ""}`}>
                      <button
                        type="button"
                        role="option"
                        aria-selected={selectedId === DEFAULT_TEMPLATE}
                        disabled={!canEdit || mutationBusy}
                        onClick={() => void selectTemplate(DEFAULT_TEMPLATE)}
                      >
                        <span>{tr("默认")}</span>
                        {defaultActive && <em>{tr("使用中")}</em>}
                      </button>
                    </div>
                  )}
                  {visibleTemplates.map((item) => (
                    <div className={`prompt-list-item${item.id === selectedId ? " selected" : ""}`} key={item.id}>
                      <button
                        type="button"
                        role="option"
                        aria-selected={item.id === selectedId}
                        disabled={!canEdit || mutationBusy}
                        onClick={() => void selectTemplate(item.id)}
                      >
                        <span>{item.name}</span>
                        {item.source !== source && sourceLabel(item.source) && <small>{sourceLabel(item.source)}</small>}
                        {data.active_template_id === item.id && <em>{tr("使用中")}</em>}
                      </button>
                      <button
                        className="prompt-list-delete"
                        type="button"
                        aria-label={tr("删除模版 {0}", { 0: item.name })}
                        disabled={!canEdit || mutationBusy}
                        onClick={() => void deleteTemplate(item)}
                      >
                        <Trash2 />
                      </button>
                    </div>
                  ))}
                  {!showDefaultTemplate && visibleTemplates.length === 0 && (
                    <p className="prompt-settings-empty">{tr("没有匹配的模版。")}</p>
                  )}
                </div>
              </div>
              <div className="prompt-settings-body">
                <label className="prompt-settings-field">
                  <span>{tr("模版名称")}</span>
                  <input
                    aria-label={tr("模版名称")}
                    value={templateName}
                    disabled={!canEdit || mutationBusy}
                    placeholder={tr("保存时使用的名称")}
                    onChange={(event) => setTemplateName(event.target.value)}
                  />
                </label>
                {data.sections.map((section) => (
                  <div className="prompt-settings-field" key={section.key}>
                    <label htmlFor={`prompt-section-${section.key}`}>{promptSectionLabel(section)}</label>
                    {section.description && <small>{section.description}</small>}
                    <textarea
                      id={`prompt-section-${section.key}`}
                      aria-label={promptSectionLabel(section)}
                      value={draft[section.key] ?? ""}
                      disabled={!canEdit || mutationBusy}
                      rows={4}
                      placeholder={section.default_text || (section.key === "extra" ? tr("留空则不追加额外段落") : tr("留空则使用默认"))}
                      onChange={(event) => setDraft((current) => ({ ...current, [section.key]: event.target.value }))}
                    />
                  </div>
                ))}
                <div className="prompt-settings-actions">
                  <button
                    className="settings-command primary"
                    type="button"
                    disabled={!canEdit || mutationBusy}
                    onClick={() => void saveTemplate()}
                  >
                    <span>{creating ? tr("保存为模版") : tr("保存模版")}</span>
                  </button>
                  {!creating && (
                    <button
                      className="settings-command"
                      type="button"
                      disabled={!canEdit || mutationBusy}
                      onClick={() => {
                        const name = templateName.trim();
                        const nextName = name.endsWith(tr(" 副本")) ? name : tr("{0} 副本", { 0: name || tr("未命名") });
                        void mutate({
                          action: "save_template",
                          source,
                          template_name: nextName,
                          sections: draft,
                        });
                      }}
                    >
                      <span>{tr("另存为新模版")}</span>
                    </button>
                  )}
                  <button
                    className="settings-command"
                    type="button"
                    disabled={mutationBusy}
                    onClick={() => void exportTemplates("current")}
                  >
                    <Download />
                    <span>{tr("导出 JSON")}</span>
                  </button>
                  <button
                    className="settings-command"
                    type="button"
                    aria-label={tr("导出全部提示词模版 JSON")}
                    disabled={mutationBusy}
                    onClick={() => void exportTemplates("all")}
                  >
                    <Download />
                    <span>{tr("导出全部")}</span>
                  </button>
                  <button
                    className="settings-command"
                    type="button"
                    disabled={!canEdit || mutationBusy}
                    onClick={() => importInputRef.current?.click()}
                  >
                    <Upload />
                    <span>{tr("导入 JSON")}</span>
                  </button>
                  <input
                    ref={importInputRef}
                    type="file"
                    accept="application/json,.json"
                    hidden
                    aria-label={tr("导入提示词模版 JSON")}
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      event.target.value = "";
                      if (file) void importTemplateFile(file);
                    }}
                  />
                </div>
              </div>
            </div>
          </section>

          <section className="runtime-settings-group settings-group" aria-labelledby="user-append-title">
            <div className="settings-subsection-heading">
              <div>
                <h3 id="user-append-title">{tr("追加到用户输入")}</h3>
                <p>{tr("添加后出现在下面的列表里。勾选的提示会追加到发送给模型的用户消息末尾，对话记录仍只显示原文。")}</p>
              </div>
            </div>
            <div className="prompt-user-pane">
              <label className="prompt-list-search">
                <Search />
                <input
                  aria-label={tr("搜索用户提示")}
                  value={promptQuery}
                  placeholder={tr("搜索用户提示")}
                  onChange={(event) => setPromptQuery(event.target.value)}
                />
              </label>
              {data.user_prompts.length === 0 ? (
                <p className="prompt-settings-empty">{tr("还没有用户提示。")}</p>
              ) : visiblePrompts.length === 0 ? (
                <p className="prompt-settings-empty">{tr("没有匹配的用户提示。")}</p>
              ) : (
                <ul className="prompt-user-list">
                  {visiblePrompts.map((item) => (
                    <li key={item.id}>
                      <label>
                        <input
                          type="checkbox"
                          aria-label={tr("追加提示：{0}", { 0: item.text })}
                          checked={item.enabled}
                          disabled={!canEdit || mutationBusy}
                          onChange={(event) => void togglePrompt(item.id, item.source, event.target.checked)}
                        />
                        <span>{item.text}</span>
                      </label>
                      <button
                        className="prompt-list-delete"
                        type="button"
                        aria-label={tr("移除提示：{0}", { 0: item.text })}
                        disabled={!canEdit || mutationBusy}
                        onClick={() => void deletePrompt(item.id, item.source)}
                      >
                        <Trash2 />
                        <span>{tr("删除")}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <form className="prompt-user-composer" onSubmit={(event) => void addPrompt(event)}>
                <textarea
                  aria-label={tr("追加提示内容")}
                  value={promptText}
                  disabled={!canEdit || mutationBusy}
                  rows={3}
                  placeholder={tr("例如：始终用中文回答，并先给出结论。")}
                  onChange={(event) => setPromptText(event.target.value)}
                />
                <button className="settings-command primary" type="submit" disabled={!canEdit || mutationBusy}>
                  <Plus />
                  <span>{tr("添加")}</span>
                </button>
              </form>
            </div>
          </section>
        </>
      )}
    </section>
  );
}
