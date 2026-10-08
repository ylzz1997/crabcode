import { randomBytes } from "crypto";
import * as vscode from "vscode";
import type { RuntimeSettingsMutationRequest, RuntimeSettingsResponse } from "./client/types";

export class ContextSettingsPanel {
  public static current: ContextSettingsPanel | undefined;
  private busy = false;
  private readonly disposables: vscode.Disposable[] = [];

  public static show(): void {
    if (this.current) {
      this.current.panel.reveal();
      void this.current.request();
      return;
    }
    this.current = new ContextSettingsPanel(vscode.window.createWebviewPanel(
      "crabcode.contextSettings", "CrabCode 上下文压缩", vscode.ViewColumn.Active,
      { enableScripts: true, retainContextWhenHidden: true },
    ));
  }

  private constructor(private readonly panel: vscode.WebviewPanel) {
    panel.webview.html = contextSettingsHtml();
    panel.onDidDispose(() => {
      ContextSettingsPanel.current = undefined;
      this.disposables.forEach((item) => item.dispose());
    }, null, this.disposables);
    panel.webview.onDidReceiveMessage((message) => {
      if (message?.type === "refresh") void this.request();
      if (message?.type === "save" && message.mutation) {
        const { source, auto_compact_enabled, compact_buffer_tokens, max_context_length } = message.mutation;
        void this.request({ action: "set_compaction", source, auto_compact_enabled, compact_buffer_tokens, max_context_length });
      }
    }, null, this.disposables);
  }

  private async request(mutation?: RuntimeSettingsMutationRequest): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    await this.panel.webview.postMessage({ type: "busy" });
    try {
      const config = vscode.workspace.getConfiguration("crabcode");
      const url = new URL(config.get<string>("serverUrl", "ws://localhost:4096/ws"));
      url.protocol = url.protocol === "wss:" ? "https:" : "http:";
      url.pathname = "/config/runtime-settings";
      url.search = "";
      url.hash = "";
      const cwd = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
      const password = config.get<string>("password", "");
      const headers: Record<string, string> = {};
      if (password) headers.Authorization = `Bearer ${password}`;
      if (mutation) headers["Content-Type"] = "application/json";
      else if (cwd) url.searchParams.set("cwd", cwd);
      const response = await fetch(url, {
        method: mutation ? "POST" : "GET", headers,
        ...(mutation ? { body: JSON.stringify({ ...mutation, cwd }) } : {}),
      });
      if (!response.ok) {
        let detail = response.statusText || `HTTP ${response.status}`;
        try {
          const body = await response.json() as { detail?: string | { msg: string }[] };
          if (typeof body.detail === "string") detail = body.detail;
          else if (Array.isArray(body.detail)) detail = body.detail.map((item) => item.msg).join("; ");
        } catch { /* Keep the HTTP error for non-JSON responses. */ }
        throw new Error(detail);
      }
      const data = await response.json() as RuntimeSettingsResponse;
      await this.panel.webview.postMessage({ type: "state", data, saved: Boolean(mutation) });
    } catch (error) {
      await this.panel.webview.postMessage({ type: "error", message: error instanceof Error ? error.message : String(error) });
    } finally {
      this.busy = false;
    }
  }
}

function contextSettingsHtml(): string {
  const nonce = randomBytes(16).toString("hex");
  return `<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
<style nonce="${nonce}">
body { font-family: var(--vscode-font-family); color: var(--vscode-foreground); background: var(--vscode-editor-background); max-width: 720px; padding: 24px; margin: auto; }
h1 { font-size: 22px; } p, small { color: var(--vscode-descriptionForeground); line-height: 1.6; }
label { display: block; margin: 20px 0 8px; } small { display: block; margin-top: 6px; }
input[type=number], select { box-sizing: border-box; padding: 8px; width: 100%; color: var(--vscode-input-foreground); background: var(--vscode-input-background); border: 1px solid var(--vscode-input-border, transparent); }
fieldset { padding: 0; border: 0; } button { cursor: pointer; padding: 8px 16px; margin: 20px 8px 0 0; border: none; background: var(--vscode-button-background); color: var(--vscode-button-foreground); }
button:disabled { opacity: .5; cursor: default; } :focus-visible { outline: 1px solid var(--vscode-focusBorder); outline-offset: 2px; }
#message { white-space: pre-wrap; } #message.error { color: var(--vscode-errorForeground); } #cwd { overflow-wrap: anywhere; }
</style></head><body>
<h1>上下文压缩</h1>
<p>读取 Gateway 的有效配置，保存到指定配置层。保存后从下一轮对话生效；正在运行的一轮沿用原设置。</p>
<p id="cwd"></p>
<form id="form"><fieldset id="fields" disabled>
<label for="source">保存到配置层</label><select id="source"></select>
<label><input id="enabled" type="checkbox"> 自动压缩</label>
<small>接近上下文上限时，整理历史内容并继续当前任务。</small>
<label for="buffer">压缩预留 token</label><input id="buffer" type="number" min="0" step="1" required value="20000">
<small>默认 20,000。实际至少预留模型的最大输出额度；设为 0 仍保留输出空间。</small>
<label for="limit">提前触发阈值（tokens）</label><input id="limit" type="number" min="1" step="1" placeholder="自动">
<small>已用 token 超过此值时提前压缩。留空自动计算，设置值不能推迟安全阈值。</small>
<p>自动阈值 = 上下文容量 − max（压缩预留 token，模型最大输出 token）。</p>
<button type="submit">保存</button>
</fieldset></form>
<button id="refresh" type="button">刷新</button>
<p id="message" role="status" aria-live="polite">正在读取…</p>
<script nonce="${nonce}">
const vscode = acquireVsCodeApi();
const byId = (id) => document.getElementById(id);
const fields = byId('fields'), source = byId('source'), enabled = byId('enabled');
const buffer = byId('buffer'), limit = byId('limit'), message = byId('message'), refresh = byId('refresh');
let writable = false;
const notice = (text, error = false) => { message.textContent = text; message.className = error ? 'error' : ''; };
window.addEventListener('message', ({ data: event }) => {
  if (event.type === 'busy') { fields.disabled = true; refresh.disabled = true; notice('正在处理…'); return; }
  refresh.disabled = false;
  if (event.type === 'error') { fields.disabled = !writable; notice(event.message, true); return; }
  if (event.type !== 'state') return;
  const data = event.data;
  const sources = (data.editable_sources || []).filter(item => item.writable);
  const selected = source.value;
  source.replaceChildren();
  for (const item of sources) { const option = document.createElement('option'); option.value = item.id; option.textContent = item.label; source.appendChild(option); }
  source.value = sources.some(item => item.id === selected) ? selected : (sources.find(item => item.id === 'projectSettings') || sources[0] || {}).id || '';
  writable = sources.length > 0;
  fields.disabled = !writable;
  enabled.checked = data.auto_compact_enabled !== false;
  buffer.value = String(data.compact_buffer_tokens ?? 20000);
  limit.value = data.max_context_length == null ? '' : String(data.max_context_length);
  byId('cwd').textContent = '项目：' + data.cwd;
  notice([event.saved ? '已保存，从下一轮对话生效。' : '', !writable ? '当前配置层不可写。' : '', ...(data.warnings || [])].filter(Boolean).join('\\n'));
});
byId('form').addEventListener('submit', event => {
  event.preventDefault();
  if (fields.disabled) return;
  const reserved = Number(buffer.value), threshold = limit.value.trim() === '' ? null : Number(limit.value);
  if (buffer.value.trim() === '' || !Number.isSafeInteger(reserved) || reserved < 0 || (threshold !== null && (!Number.isSafeInteger(threshold) || threshold < 1))) {
    notice('预留 token 必须是非负整数；提前触发阈值须为正整数或留空。', true); return;
  }
  fields.disabled = true;
  refresh.disabled = true;
  vscode.postMessage({ type: 'save', mutation: { source: source.value, auto_compact_enabled: enabled.checked, compact_buffer_tokens: reserved, max_context_length: threshold } });
});
refresh.addEventListener('click', () => vscode.postMessage({ type: 'refresh' }));
vscode.postMessage({ type: 'refresh' });
</script></body></html>`;
}
