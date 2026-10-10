import * as os from "os";
import * as vscode from "vscode";
import {
  parsePromptTemplateFile,
  promptTemplateFilename,
  serializePromptTemplate,
  serializePromptTemplates,
} from "./promptTemplateFile";

interface PromptSection {
  key: string;
  label: string;
  description?: string | null;
  default_text?: string | null;
}

interface PromptTemplate {
  id: string;
  name: string;
  sections: Record<string, string>;
  source: string;
}

interface UserPrompt {
  id: string;
  text: string;
  enabled: boolean;
  source: string;
}

interface EditableSource {
  id: "userSettings" | "projectSettings" | "localSettings";
  label: string;
  writable: boolean;
}

interface PromptSettingsPayload {
  active_template_id: string | null;
  templates: PromptTemplate[];
  user_prompts: UserPrompt[];
  sections: PromptSection[];
  warnings: string[];
  editable_sources?: EditableSource[];
}

type PromptMutation = {
  action: string;
  source: EditableSource["id"];
  template_id?: string | null;
  template_name?: string;
  sections?: Record<string, string>;
  prompt_id?: string;
  prompt_text?: string;
  enabled?: boolean;
};

function gatewayUrl(path: string): string {
  const wsUrl = vscode.workspace.getConfiguration("crabcode").get<string>("serverUrl", "ws://localhost:4096/ws");
  const url = new URL(wsUrl);
  url.protocol = url.protocol === "wss:" ? "https:" : "http:";
  url.pathname = path;
  url.search = "";
  return url.toString();
}

function gatewayHeaders(): Record<string, string> {
  const password = vscode.workspace.getConfiguration("crabcode").get<string>("password", "");
  const headers: Record<string, string> = {};
  if (password) headers.Authorization = `Bearer ${password}`;
  return headers;
}

function workspaceCwd(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

async function readError(response: Response): Promise<string> {
  const fallback = response.statusText || `HTTP ${response.status}`;
  try {
    const payload = await response.json() as { detail?: unknown };
    if (typeof payload.detail === "string" && payload.detail) return payload.detail;
    if (Array.isArray(payload.detail)) {
      return payload.detail.map((item) => {
        if (item && typeof item === "object" && "msg" in item) return String((item as { msg: unknown }).msg);
        return String(item);
      }).join("; ");
    }
  } catch {
    // The body was not JSON.
  }
  return fallback;
}

export class PromptSettingsPanel {
  public static current: PromptSettingsPanel | undefined;
  private readonly panel: vscode.WebviewPanel;
  private readonly disposables: vscode.Disposable[] = [];

  public static show(): void {
    if (PromptSettingsPanel.current) {
      PromptSettingsPanel.current.panel.reveal();
      void PromptSettingsPanel.current.refresh();
      return;
    }
    const panel = vscode.window.createWebviewPanel(
      "crabcode.promptSettings",
      "CrabCode 提示词",
      vscode.ViewColumn.Active,
      { enableScripts: true, retainContextWhenHidden: true },
    );
    PromptSettingsPanel.current = new PromptSettingsPanel(panel);
  }

  private constructor(panel: vscode.WebviewPanel) {
    this.panel = panel;
    this.panel.webview.html = html();
    this.panel.onDidDispose(() => this.dispose(), null, this.disposables);
    this.panel.webview.onDidReceiveMessage(
      (message: {
        type?: string;
        mutation?: PromptMutation;
        source?: string;
        template?: unknown;
        templates?: unknown;
      }) => {
        if (message?.type === "refresh") void this.refresh();
        if (message?.type === "mutate" && message.mutation) void this.mutate(message.mutation);
        if (message?.type === "export-current") void this.exportTemplates("current", message.template);
        if (message?.type === "export-all") void this.exportTemplates("all", message.templates);
        if (message?.type === "import" && message.source) void this.importTemplates(message.source);
      },
      null,
      this.disposables,
    );
  }

  private dispose(): void {
    PromptSettingsPanel.current = undefined;
    this.panel.dispose();
    for (const item of this.disposables) item.dispose();
  }

  private async refresh(): Promise<void> {
    try {
      const url = new URL(gatewayUrl("/config/prompt-settings"));
      const cwd = workspaceCwd();
      if (cwd) url.searchParams.set("cwd", cwd);
      const response = await fetch(url, { headers: gatewayHeaders() });
      if (!response.ok) throw new Error(await readError(response));
      const data = await response.json() as PromptSettingsPayload;
      await this.panel.webview.postMessage({ type: "state", data });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      await this.panel.webview.postMessage({ type: "error", message });
    }
  }

  private async mutate(mutation: PromptMutation): Promise<void> {
    try {
      await this.saveMutation(mutation);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      await this.panel.webview.postMessage({ type: "error", message });
    }
  }

  private async saveMutation(mutation: PromptMutation): Promise<void> {
    const response = await fetch(gatewayUrl("/config/prompt-settings"), {
      method: "POST",
      headers: { ...gatewayHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ ...mutation, cwd: workspaceCwd() }),
    });
    if (!response.ok) throw new Error(await readError(response));
    const data = await response.json() as PromptSettingsPayload;
    await this.panel.webview.postMessage({ type: "state", data });
  }

  private async exportTemplates(kind: "current" | "all", payload: unknown): Promise<void> {
    try {
      const wrapped = kind === "all" ? { templates: payload } : payload;
      const templates = parsePromptTemplateFile(JSON.stringify(wrapped));
      const text = kind === "all" ? serializePromptTemplates(templates) : serializePromptTemplate(templates[0]);
      const filename = kind === "all" ? "prompt-templates.json" : promptTemplateFilename(templates[0].name);
      const folder = vscode.workspace.workspaceFolders?.[0]?.uri ?? vscode.Uri.file(os.homedir());
      const uri = await vscode.window.showSaveDialog({
        title: kind === "all" ? "导出全部提示词模版" : "导出提示词模版",
        defaultUri: vscode.Uri.joinPath(folder, filename),
        filters: { JSON: ["json"] },
        saveLabel: "导出",
      });
      if (!uri) return;
      await vscode.workspace.fs.writeFile(uri, Buffer.from(text, "utf8"));
      await this.panel.webview.postMessage({ type: "notice", message: `已导出到 ${uri.fsPath}` });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      await this.panel.webview.postMessage({ type: "error", message });
    }
  }

  private async importTemplates(source: string): Promise<void> {
    if (source !== "userSettings" && source !== "projectSettings" && source !== "localSettings") {
      await this.panel.webview.postMessage({ type: "error", message: "请选择要写入的配置层" });
      return;
    }
    const picked = await vscode.window.showOpenDialog({
      title: "导入提示词模版",
      canSelectMany: false,
      openLabel: "导入",
      filters: { JSON: ["json"] },
    });
    if (!picked?.length) return;
    try {
      const bytes = await vscode.workspace.fs.readFile(picked[0]);
      const templates = parsePromptTemplateFile(new TextDecoder("utf-8").decode(bytes));
      let imported = 0;
      try {
        for (const template of templates) {
          await this.saveMutation({
            action: "save_template",
            source,
            template_id: template.id,
            template_name: template.name,
            sections: template.sections,
          });
          imported += 1;
        }
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error);
        const message = imported > 0 ? `已导入 ${imported} 个，随后失败：${detail}` : detail;
        await this.panel.webview.postMessage({ type: "error", message });
        return;
      }
      const message = templates.length === 1 ? `已导入 ${templates[0].name}` : `已导入 ${templates.length} 个模版`;
      await this.panel.webview.postMessage({ type: "notice", message });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      await this.panel.webview.postMessage({ type: "error", message });
    }
  }
}

function html(): string {
  const nonce = `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>CrabCode 提示词</title>
<style>
  body { margin: 0; padding: 20px 24px 40px; color: var(--vscode-foreground); font-family: var(--vscode-font-family); font-size: 13px; line-height: 1.5; }
  h1 { margin: 0 0 4px; font-size: 18px; font-weight: 600; }
  h2 { margin: 0 0 4px; font-size: 14px; }
  p { margin: 0; color: var(--vscode-descriptionForeground); }
  header, .toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  section { margin-top: 18px; padding: 14px; border: 1px solid var(--vscode-panel-border, transparent); border-radius: 8px; background: var(--vscode-editor-background); }
  label.field { display: flex; flex-direction: column; gap: 6px; margin-top: 12px; }
  input, select, textarea, button { font: inherit; color: inherit; }
  input, select, textarea { box-sizing: border-box; width: 100%; padding: 6px 8px; border: 1px solid var(--vscode-input-border, transparent); border-radius: 4px; background: var(--vscode-input-background); color: var(--vscode-input-foreground); }
  textarea { min-height: 72px; resize: vertical; }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  button { padding: 4px 10px; border: 1px solid var(--vscode-button-border, transparent); border-radius: 4px; background: var(--vscode-button-secondaryBackground); color: var(--vscode-button-secondaryForeground); cursor: pointer; }
  button.primary { background: var(--vscode-button-background); color: var(--vscode-button-foreground); }
  button:disabled { opacity: .5; cursor: default; }
  .note, .error { margin-top: 10px; padding: 8px 10px; border-radius: 4px; }
  .note { background: var(--vscode-textBlockQuote-background); }
  .error { background: var(--vscode-inputValidation-errorBackground); color: var(--vscode-errorForeground); }
  ul { list-style: none; margin: 12px 0 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
  li { display: flex; align-items: flex-start; gap: 8px; padding: 8px; border-radius: 6px; background: var(--vscode-input-background); }
  li label { flex: 1; display: flex; gap: 8px; white-space: pre-wrap; }
  li input { width: auto; margin-top: 3px; }
  .source { margin-left: auto; min-width: 180px; }
</style>
</head>
<body>
  <header>
    <div>
      <h1>提示词</h1>
      <p>自定义系统与上下文压缩提示词模版，并选择要追加到用户输入的提示。</p>
    </div>
    <button type="button" id="refresh">刷新</button>
  </header>
  <div id="banner"></div>
  <div class="toolbar">
    <p id="status">正在读取…</p>
    <label class="source">写入层
      <select id="source"></select>
    </label>
  </div>
  <section>
    <h2>自定义提示词模版</h2>
    <p>留空的段落使用内置默认。第一个选项是「默认」，未选择模版时也使用它。若配置里已有 prompt_profile，选择默认时仍会沿用它。导入和导出走 JSON，写入当前选择的配置层。</p>
    <label class="field">使用模版
      <select id="template"></select>
    </label>
    <label class="field">模版名称
      <input id="name" placeholder="保存时使用的名称" />
    </label>
    <div id="fields"></div>
    <div class="actions">
      <button type="button" class="primary" id="save">保存为模版</button>
      <button type="button" id="save-as">另存为新模版</button>
      <button type="button" id="remove-template">删除模版</button>
      <button type="button" id="export">导出 JSON</button>
      <button type="button" id="export-all">导出全部</button>
      <button type="button" id="import">导入 JSON</button>
    </div>
  </section>
  <section>
    <h2>追加到用户输入</h2>
    <p>添加后保存在列表中。勾选的提示会追加到发送给模型的用户消息末尾，对话记录仍只显示原文。</p>
    <label class="field">用户提示
      <textarea id="prompt-text" rows="3" placeholder="例如：始终用中文回答，并先给出结论。"></textarea>
    </label>
    <div class="actions"><button type="button" class="primary" id="add">添加</button></div>
    <ul id="prompts"></ul>
  </section>
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    const DEFAULT_ID = "default";
    let state = null;
    let selectedId = DEFAULT_ID;
    let draft = {};
    let templateStamp = "";
    const banner = document.getElementById("banner");
    const status = document.getElementById("status");
    const source = document.getElementById("source");
    const template = document.getElementById("template");
    const nameInput = document.getElementById("name");
    const fields = document.getElementById("fields");
    const prompts = document.getElementById("prompts");
    const saveButton = document.getElementById("save");
    const saveAsButton = document.getElementById("save-as");
    const removeButton = document.getElementById("remove-template");

    function writableSources() {
      return (state && state.editable_sources || []).filter((item) => item.writable);
    }

    function currentSource() {
      return source.value || (writableSources()[0] && writableSources()[0].id) || "userSettings";
    }

    function selectedTemplate() {
      return state && state.templates.find((item) => item.id === selectedId);
    }

    function showError(message) {
      banner.innerHTML = '<div class="error"></div>';
      banner.firstChild.textContent = message;
    }

    function clearError() { banner.textContent = ""; }

    function loadSelection(id) {
      selectedId = id;
      const current = state && state.templates.find((item) => item.id === id);
      nameInput.value = current ? current.name : "";
      draft = {};
      (state ? state.sections : []).forEach((section) => {
        draft[section.key] = current && current.sections[section.key] ? current.sections[section.key] : "";
      });
      renderFields();
      saveButton.textContent = id === DEFAULT_ID ? "保存为模版" : "保存模版";
      saveAsButton.hidden = id === DEFAULT_ID;
      removeButton.hidden = id === DEFAULT_ID;
    }

    function renderFields() {
      fields.textContent = "";
      (state ? state.sections : []).forEach((section) => {
        const label = document.createElement("label");
        label.className = "field";
        label.append(section.label);
        if (section.description) {
          const description = document.createElement("small");
          description.textContent = section.description;
          label.append(description);
        }
        const area = document.createElement("textarea");
        area.setAttribute("aria-label", section.label);
        area.value = draft[section.key] || "";
        area.disabled = writableSources().length === 0;
        area.placeholder = section.default_text || (section.key === "extra" ? "留空则不追加额外段落" : "留空则使用默认");
        area.addEventListener("input", () => { draft[section.key] = area.value; });
        label.append(area);
        fields.append(label);
      });
    }

    function render() {
      const sources = writableSources();
      const previous = source.value;
      source.textContent = "";
      sources.forEach((item) => {
        const option = document.createElement("option");
        option.value = item.id;
        option.textContent = item.label;
        source.append(option);
      });
      if (sources.some((item) => item.id === previous)) source.value = previous;
      source.disabled = sources.length === 0;
      template.textContent = "";
      const fallback = document.createElement("option");
      fallback.value = DEFAULT_ID;
      fallback.textContent = "默认";
      template.append(fallback);
      (state ? state.templates : []).forEach((item) => {
        const option = document.createElement("option");
        option.value = item.id;
        const owner = sources.find((entry) => entry.id === item.source);
        option.textContent = owner && owner.id !== currentSource() ? item.name + " · " + owner.label : item.name;
        template.append(option);
      });
      const active = state && state.active_template_id;
      const known = !active || (state.templates || []).some((item) => item.id === active);
      const nextId = known && active ? active : DEFAULT_ID;
      const stamp = JSON.stringify({
        id: nextId,
        templates: state ? state.templates : [],
        sections: state ? state.sections : [],
      });
      if (stamp !== templateStamp) {
        templateStamp = stamp;
        loadSelection(nextId);
      }
      template.value = selectedId;
      prompts.textContent = "";
      const items = state ? state.user_prompts : [];
      if (!items.length) {
        const empty = document.createElement("li");
        empty.textContent = "还没有用户提示。";
        prompts.append(empty);
      }
      items.forEach((item) => {
        const row = document.createElement("li");
        const label = document.createElement("label");
        const box = document.createElement("input");
        box.type = "checkbox";
        box.checked = item.enabled;
        const writable = sources.some((entry) => entry.id === item.source);
        box.disabled = !writable;
        box.addEventListener("change", () => mutate({
          action: "set_user_prompt_enabled",
          source: item.source,
          prompt_id: item.id,
          enabled: box.checked,
        }));
        const text = document.createElement("span");
        text.textContent = item.text;
        label.append(box, text);
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "移除";
        remove.disabled = !writable;
        remove.addEventListener("click", () => {
          if (!confirm("从列表中移除这条用户提示？")) return;
          mutate({ action: "delete_user_prompt", source: item.source, prompt_id: item.id });
        });
        row.append(label, remove);
        prompts.append(row);
      });
      const locked = sources.length === 0;
      document.getElementById("import").disabled = locked;
      saveButton.disabled = locked;
      saveAsButton.disabled = locked;
      removeButton.disabled = locked || !(selectedTemplate() && sources.some((item) => item.id === selectedTemplate().source));
      document.getElementById("add").disabled = locked;
      status.textContent = state ? "已连接到 Gateway" : "尚未读取到提示词设置";
    }

    function mutate(mutation) {
      clearError();
      vscode.postMessage({ type: "mutate", mutation });
    }

    function dirty() {
      if (!state) return false;
      const current = selectedTemplate();
      const savedName = current ? current.name : "";
      if (nameInput.value !== savedName) return true;
      return (state.sections || []).some((section) => {
        const saved = current && current.sections[section.key] ? current.sections[section.key] : "";
        return (draft[section.key] || "") !== saved;
      });
    }

    template.addEventListener("change", () => {
      const id = template.value;
      if (id !== selectedId && dirty() && !confirm("当前修改尚未保存为模版，切换后会丢弃这些修改。")) {
        template.value = selectedId;
        return;
      }
      loadSelection(id);
      templateStamp = "";
      mutate({
        action: "set_active_template",
        source: currentSource(),
        template_id: id === DEFAULT_ID ? null : id,
      });
    });
    saveButton.addEventListener("click", () => {
      const name = nameInput.value.trim();
      if (!name) { showError("请先填写模版名称"); return; }
      mutate({
        action: "save_template",
        source: currentSource(),
        template_id: selectedId === DEFAULT_ID ? undefined : selectedId,
        template_name: name,
        sections: draft,
      });
    });
    saveAsButton.addEventListener("click", () => {
      const suggested = nameInput.value.trim() ? nameInput.value.trim() + " 副本" : "";
      const name = prompt("新模版名称", suggested);
      if (!name || !name.trim()) return;
      mutate({
        action: "save_template",
        source: currentSource(),
        template_name: name.trim(),
        sections: draft,
      });
    });
    removeButton.addEventListener("click", () => {
      const current = selectedTemplate();
      if (!current || !confirm("删除提示词模版「" + current.name + "」？")) return;
      mutate({ action: "delete_template", source: current.source, template_id: current.id });
    });
    function cleanedSections() {
      const sections = {};
      Object.keys(draft).forEach((key) => {
        const text = (draft[key] || "").trim();
        if (text) sections[key] = text;
      });
      return sections;
    }
    function currentTemplate() {
      const template = { name: nameInput.value.trim(), sections: cleanedSections() };
      if (selectedId !== DEFAULT_ID) template.id = selectedId;
      return template;
    }
    document.getElementById("export").addEventListener("click", () => {
      if (!nameInput.value.trim()) { showError("请先填写模版名称"); return; }
      clearError();
      vscode.postMessage({ type: "export-current", template: currentTemplate() });
    });
    document.getElementById("export-all").addEventListener("click", () => {
      const templates = (state ? state.templates : []).map((item) => {
        if (item.id === selectedId) {
          return { id: item.id, name: nameInput.value.trim() || item.name, sections: cleanedSections() };
        }
        return { id: item.id, name: item.name, sections: item.sections || {} };
      });
      if (selectedId === DEFAULT_ID && nameInput.value.trim()) templates.push(currentTemplate());
      if (!templates.length) { showError("还没有可导出的模版"); return; }
      clearError();
      vscode.postMessage({ type: "export-all", templates });
    });
    document.getElementById("import").addEventListener("click", () => {
      if (dirty() && !confirm("当前修改尚未保存为模版，导入后会丢弃这些修改。")) return;
      clearError();
      vscode.postMessage({ type: "import", source: currentSource() });
    });
    document.getElementById("add").addEventListener("click", () => {
      const text = document.getElementById("prompt-text").value.trim();
      if (!text) { showError("用户提示不能为空"); return; }
      document.getElementById("prompt-text").value = "";
      mutate({ action: "add_user_prompt", source: currentSource(), prompt_text: text });
    });
    document.getElementById("refresh").addEventListener("click", () => {
      status.textContent = "正在读取…";
      vscode.postMessage({ type: "refresh" });
    });
    window.addEventListener("message", (event) => {
      const message = event.data || {};
      if (message.type === "notice") {
        banner.innerHTML = '<div class="note"></div>';
        banner.firstChild.textContent = message.message || "";
        return;
      }
      if (message.type === "error") {
        showError(message.message || "读取提示词设置失败");
        status.textContent = "读取失败";
        return;
      }
      if (message.type === "state") {
        clearError();
        state = message.data;
        (state.warnings || []).forEach(showError);
        render();
      }
    });
    vscode.postMessage({ type: "refresh" });
  </script>
</body>
</html>`;
}
