import { randomBytes } from "crypto";
import * as os from "os";
import * as vscode from "vscode";
import {
  LOCAL_SETTINGS,
  validateLocalSetting,
  type Section,
} from "./settings/catalog";
import type {
  Resource,
  SettingsRequest,
  SettingsSnapshot,
} from "./settings/protocol";
import {
  parsePromptTemplateFile,
  promptTemplateFilename,
  serializePromptTemplate,
  serializePromptTemplates,
} from "./promptTemplateFile";

const ENDPOINTS: Record<Resource, string> = {
  runtime: "/config/runtime-settings",
  models: "/config/model-settings",
  prompts: "/config/prompt-settings",
  usage: "/usage/daily",
};
const ACTIONS: Record<string, readonly string[]> = {
  runtime: [
    "set_snapshot",
    "set_compaction",
    "set_computer_use_options",
    "add_extra_tool",
    "remove_extra_tool",
  ],
  models: [
    "upsert_model",
    "delete_model",
    "upsert_group",
    "delete_group",
    "set_default_model",
    "clear_default_model",
  ],
  prompts: [
    "save_template",
    "delete_template",
    "set_active_template",
    "add_user_prompt",
    "set_user_prompt_enabled",
    "delete_user_prompt",
  ],
};

/** One settings editor; credentials and all filesystem/network work stay in the extension host. */
export class CrabCodeSettingsPanel {
  static current: CrabCodeSettingsPanel | undefined;
  private generation = 0;
  private identity = "";
  private disposed = false;
  private queue: Promise<unknown> = Promise.resolve();
  private readonly disposables: vscode.Disposable[] = [];
  private readonly requests = new Set<AbortController>();
  private section: Section;

  static show(
    extensionUri: vscode.Uri,
    section: Section = "general",
    onModelsChanged?: () => void,
  ): void {
    if (this.current) {
      this.current.section = section;
      this.current.panel.reveal();
      void this.current.panel.webview.postMessage({
        type: "navigate",
        section,
      });
      this.current.publishSnapshot();
      return;
    }
    const panel = vscode.window.createWebviewPanel(
      "crabcode.settings",
      "CrabCode设置",
      vscode.ViewColumn.Active,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [vscode.Uri.joinPath(extensionUri, "dist")],
      },
    );
    this.current = new CrabCodeSettingsPanel(
      panel,
      extensionUri,
      section,
      onModelsChanged,
    );
  }

  private constructor(
    private readonly panel: vscode.WebviewPanel,
    extensionUri: vscode.Uri,
    section: Section,
    private readonly onModelsChanged?: () => void,
  ) {
    this.section = section;
    this.snapshot();
    const nonce = randomBytes(16).toString("hex");
    const script = panel.webview.asWebviewUri(
      vscode.Uri.joinPath(extensionUri, "dist", "settings.js"),
    );
    const css = panel.webview.asWebviewUri(
      vscode.Uri.joinPath(extensionUri, "dist", "settings.css"),
    );
    panel.webview.html = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${panel.webview.cspSource}; script-src 'nonce-${nonce}';"><link rel="stylesheet" href="${css}"><title>CrabCode设置</title></head><body><div id="app"></div><script nonce="${nonce}" src="${script}"></script></body></html>`;
    panel.webview.onDidReceiveMessage(
      (message: SettingsRequest) => {
        if (message?.type !== "request" || !Number.isSafeInteger(message.id))
          return;
        // Local preferences stay usable even while an offline Gateway times out.
        if (!["read", "mutate", "import"].includes(message.action)) {
          void this.handle(message);
          return;
        }
        // Gateway writes and reads are ordered to avoid stale refresh results.
        this.queue = this.queue.then(
          () => this.handle(message),
          () => this.handle(message),
        );
      },
      null,
      this.disposables,
    );
    vscode.workspace.onDidChangeConfiguration(
      (event) => {
        if (event.affectsConfiguration("crabcode")) this.publishSnapshot();
      },
      null,
      this.disposables,
    );
    vscode.workspace.onDidChangeWorkspaceFolders(
      () => this.publishSnapshot(),
      null,
      this.disposables,
    );
    panel.onDidDispose(
      () => {
        this.disposed = true;
        CrabCodeSettingsPanel.current = undefined;
        this.requests.forEach((controller) => controller.abort());
        this.disposables.forEach((item) => item.dispose());
      },
      null,
      this.disposables,
    );
  }

  private snapshot(): SettingsSnapshot {
    const config = vscode.workspace.getConfiguration("crabcode");
    const folder = vscode.workspace.workspaceFolders?.[0];
    const identity = JSON.stringify([
      config.get("serverUrl"),
      config.get("password"),
      folder?.uri.toString(),
      vscode.workspace.workspaceFile?.toString(),
    ]);
    if (identity !== this.identity) {
      this.identity = identity;
      this.generation++;
      this.requests.forEach((controller) => controller.abort());
    }
    const local: SettingsSnapshot["local"] = {};
    for (const spec of LOCAL_SETTINGS) {
      const inspected = config.inspect(spec.key);
      const explicit = inspected?.workspaceValue ?? inspected?.globalValue;
      // These options intentionally inherit Gateway unless explicitly configured.
      const value = spec.key.startsWith("computerUse")
        ? (explicit ?? "")
        : config.get(spec.key, spec.default);
      local[spec.key] = {
        value,
        user: inspected?.globalValue,
        workspace: inspected?.workspaceValue,
      };
    }
    let gateway = "连接地址无效，请检查扩展设置";
    try {
      gateway = new URL(config.get("serverUrl", "ws://localhost:4096/ws")).host;
    } catch {
      /* Local settings remain usable. */
    }
    return {
      generation: this.generation,
      cwd: folder?.uri.fsPath,
      gateway,
      hasWorkspace: Boolean(folder || vscode.workspace.workspaceFile),
      hasFolder: false,
      local,
    };
  }

  private publishSnapshot(): void {
    if (this.disposed) return;
    void this.panel.webview.postMessage({
      type: "snapshot",
      data: this.snapshot(),
    });
  }

  private async handle(message: SettingsRequest): Promise<void> {
    if (this.disposed) return;
    let generation = this.generation;
    try {
      this.snapshot();
      generation = this.generation;
      if (message.action !== "initialize" && message.generation !== generation)
        throw new Error("连接或工作区已切换，请刷新后重试");
      const data = await this.dispatch(message);
      if (generation !== this.generation || this.disposed) return;
      await this.panel.webview.postMessage({
        type: "response",
        id: message.id,
        generation,
        data,
      });
      if (message.action === "initialize")
        await this.panel.webview.postMessage({
          type: "navigate",
          section: this.section,
        });
    } catch (error) {
      if (!this.disposed)
        await this.panel.webview.postMessage({
          type: "response",
          id: message.id,
          generation,
          error: error instanceof Error ? error.message : String(error),
        });
    }
  }

  private async dispatch(message: SettingsRequest): Promise<unknown> {
    if (message.action === "initialize") return this.snapshot();
    if (message.action === "extension")
      return vscode.commands.executeCommand("crabcode.openSettings");
    if (message.action === "confirm")
      return (
        (await vscode.window.showWarningMessage(
          String(message.message ?? "确认操作？"),
          { modal: true },
          "确认",
        )) === "确认"
      );
    if (message.action === "saveLocal") {
      const value =
        message.reset ||
        (message.key?.startsWith("computerUse") && message.value === "")
          ? undefined
          : message.value;
      validateLocalSetting(message.key ?? "", value);
      if (message.scope !== "user" && message.scope !== "workspace")
        throw new Error("请选择有效的保存范围");
      if (message.scope === "workspace" && !this.snapshot().hasWorkspace)
        throw new Error("请先打开工作区");
      await vscode.workspace
        .getConfiguration("crabcode")
        .update(
          message.key!,
          value,
          message.scope === "user"
            ? vscode.ConfigurationTarget.Global
            : vscode.ConfigurationTarget.Workspace,
        );
      return this.snapshot();
    }
    if (message.action === "export") {
      const templates = parsePromptTemplateFile(
        JSON.stringify({ templates: message.templates }),
      );
      const uri = await vscode.window.showSaveDialog({
        title: "导出提示词模板",
        defaultUri: vscode.Uri.joinPath(
          vscode.workspace.workspaceFolders?.[0]?.uri ??
            vscode.Uri.file(os.homedir()),
          templates.length === 1
            ? promptTemplateFilename(templates[0].name)
            : "prompt-templates.json",
        ),
        filters: { JSON: ["json"] },
      });
      if (!uri) return { cancelled: true };
      await vscode.workspace.fs.writeFile(
        uri,
        Buffer.from(
          templates.length === 1
            ? serializePromptTemplate(templates[0])
            : serializePromptTemplates(templates),
        ),
      );
      return { saved: true };
    }
    if (message.action === "import") {
      this.validateSource(message.source);
      const uris = await vscode.window.showOpenDialog({
        title: "导入提示词模板",
        canSelectMany: false,
        filters: { JSON: ["json"] },
      });
      if (!uris?.length) return { cancelled: true };
      if (message.generation !== this.generation)
        throw new Error("连接或工作区已切换，请重新导入");
      const templates = parsePromptTemplateFile(
        Buffer.from(await vscode.workspace.fs.readFile(uris[0])).toString(
          "utf8",
        ),
      );
      let imported = 0;
      try {
        for (const template of templates) {
          if (message.generation !== this.generation)
            throw new Error("连接或工作区已切换");
          await this.request("prompts", {
            action: "save_template",
            source: message.source,
            template_id: template.id,
            template_name: template.name,
            sections: template.sections,
          });
          imported++;
        }
      } catch (error) {
        throw new Error(
          `已导入 ${imported} 个模板；${error instanceof Error ? error.message : String(error)}`,
        );
      }
      return { imported };
    }
    const resource = message.resource;
    if (!resource || !Object.hasOwn(ENDPOINTS, resource))
      throw new Error("未知的设置栏目");
    if (message.action === "read")
      return this.request(resource, undefined, message.query);
    if (message.action === "mutate") {
      const mutation = message.mutation;
      if (!mutation || !ACTIONS[resource]?.includes(String(mutation.action)))
        throw new Error("无效的设置操作");
      this.validateSource(mutation.source);
      const result = await this.request(resource, mutation);
      if (resource === "models") this.onModelsChanged?.();
      return result;
    }
    throw new Error("未知的设置请求");
  }

  private validateSource(source: unknown): void {
    if (
      !["userSettings", "projectSettings", "localSettings"].includes(
        String(source),
      )
    )
      throw new Error("请选择有效的配置层");
  }

  private async request(
    resource: Resource,
    mutation?: Record<string, unknown>,
    query?: Record<string, string>,
  ): Promise<unknown> {
    const config = vscode.workspace.getConfiguration("crabcode");
    const url = new URL(config.get("serverUrl", "ws://localhost:4096/ws"));
    url.protocol =
      url.protocol === "wss:" || url.protocol === "https:" ? "https:" : "http:";
    url.pathname = ENDPOINTS[resource];
    url.search = "";
    url.hash = "";
    const cwd = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (
      !mutation &&
      cwd &&
      (resource !== "usage" || query?.scope === "project")
    )
      url.searchParams.set("cwd", cwd);
    if (resource === "usage") {
      if (query?.scope === "project" && !cwd) throw new Error("请先打开项目");
      for (const key of ["start_date", "end_date", "timezone"])
        if (query?.[key]) url.searchParams.set(key, query[key]);
    }
    const headers: Record<string, string> = {};
    const password = config.get<string>("password", "");
    if (password) headers.Authorization = `Bearer ${password}`;
    if (mutation) headers["Content-Type"] = "application/json";
    const controller = new AbortController();
    this.requests.add(controller);
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(url, {
        method: mutation ? "POST" : "GET",
        headers,
        signal: controller.signal,
        ...(mutation ? { body: JSON.stringify({ ...mutation, cwd }) } : {}),
      });
      if (!response.ok) {
        let detail = `HTTP ${response.status}`;
        try {
          const body = (await response.json()) as { detail?: unknown };
          if (typeof body.detail === "string") detail = body.detail;
          else if (Array.isArray(body.detail))
            detail = body.detail
              .map((item) => item.msg ?? String(item))
              .join("; ");
        } catch {
          /* Keep status when the server sends a non-JSON error. */
        }
        if (response.status === 404 && !mutation)
          detail = "当前 Gateway 不支持此设置接口，请升级 Gateway 后重试。";
        throw new Error(detail);
      }
      return await response.json();
    } catch (error) {
      if (error instanceof Error && error.name === "AbortError")
        throw new Error("请求已取消或超时，请检查连接后重试");
      throw error;
    } finally {
      clearTimeout(timer);
      this.requests.delete(controller);
    }
  }
}
