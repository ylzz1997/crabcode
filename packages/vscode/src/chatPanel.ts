/**
 * Webview-based chat panel that renders in the CrabCode sidebar.
 *
 * The panel is a `WebviewViewProvider` so it lives inside the
 * activity-bar sidebar defined in package.json (`crabcode.chatPanel`).
 * It communicates with the extension host via `postMessage` and
 * forwards chat traffic to / from the WebSocket connection.
 *
 * P2 features: tool use/result cards with collapsible bodies, diff
 * rendering, file-change notifications.
 */

import * as vscode from "vscode";
import type { CrabCodeConnection } from "./connection";
import type {
  EventPayload,
  ToolUsePayload,
  ToolResultPayload,
  FileChangePayload,
} from "./client/types";

// ── Chat message stored locally for rendering ─────────────────────

export type ChatMessageRole = "user" | "assistant" | "system";

export interface ChatMessage {
  id: string;
  role: ChatMessageRole;
  text: string;
  timestamp: number;
}

export interface ToolCard {
  id: string;          // tool_use_id
  toolName: string;
  input: Record<string, unknown>;
  result: string | null;
  isError: boolean;
  collapsed: boolean;
}

// ── Provider ──────────────────────────────────────────────────────

export class ChatPanelProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = "crabcode.chatPanel";

  private view: vscode.WebviewView | undefined;
  private messages: ChatMessage[] = [];
  private toolCards = new Map<string, ToolCard>();

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly connection: CrabCodeConnection,
  ) {
    // Forward server events to the webview
    connection.on("message", (payload: EventPayload) => {
      this.handleServerEvent(payload);
    });
  }

  // ── WebviewViewProvider ────────────────────────────────────────

  public resolveWebviewView(
    webviewView: vscode.WebviewView,
    _context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken,
  ): void {
    this.view = webviewView;

    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this.extensionUri],
    };

    webviewView.webview.html = this.getHtmlForWebview(webviewView.webview);

    // Handle messages from the webview
    webviewView.webview.onDidReceiveMessage((msg: any) => {
      switch (msg.type) {
        case "sendMessage":
          this.handleUserMessage(msg.text);
          break;
        case "requestHistory":
          this.postMessage({ type: "history", messages: this.messages });
          break;
        case "toggleToolCard":
          this.toggleToolCard(msg.id);
          break;
        case "openFile":
          this.openFile(msg.path, msg.line);
          break;
      }
    });
  }

  // ── Public API used by commands ────────────────────────────────

  /** Reveal the chat panel in the sidebar. */
  public reveal(): void {
    if (this.view) {
      this.view.show?.(true);
    } else {
      vscode.commands.executeCommand("crabcode.chatPanel.focus");
    }
  }

  /** Send a pre-composed prompt (e.g. from context-menu commands). */
  public sendPrompt(text: string): void {
    this.addMessage("user", text);
    this.connection.send(text);
    this.reveal();
  }

  /** Pre-fill the input box without sending. */
  public prefillInput(text: string): void {
    this.postMessage({ type: "prefill", text });
    this.reveal();
  }

  // ── Internals ──────────────────────────────────────────────────

  private handleUserMessage(text: string): void {
    this.addMessage("user", text);
    this.connection.send(text);
  }

  private handleServerEvent(payload: EventPayload): void {
    switch (payload.type) {
      case "stream_text":
        this.appendAssistantText(payload.text);
        break;
      case "thinking":
        this.appendAssistantText(`[thinking] ${payload.text}`);
        break;
      case "tool_use":
        this.handleToolUse(payload as ToolUsePayload);
        break;
      case "tool_result":
        this.handleToolResult(payload as ToolResultPayload);
        break;
      case "file_change":
        this.handleFileChange(payload as FileChangePayload);
        break;
      case "error":
        this.addMessage("system", `⚠ ${payload.message}`);
        break;
      case "turn_complete":
        break;
    }
  }

  private handleToolUse(payload: ToolUsePayload): void {
    const card: ToolCard = {
      id: payload.tool_use_id,
      toolName: payload.tool_name,
      input: payload.tool_input,
      result: null,
      isError: false,
      collapsed: false,
    };
    this.toolCards.set(payload.tool_use_id, card);
    this.postMessage({ type: "toolUse", card });
  }

  private handleToolResult(payload: ToolResultPayload): void {
    const card = this.toolCards.get(payload.tool_use_id);
    if (card) {
      card.result = payload.result_for_display ?? payload.result;
      card.isError = payload.is_error ?? false;
      card.collapsed = true; // Auto-collapse once result arrives
      this.postMessage({ type: "toolResult", card });
    }
  }

  private handleFileChange(payload: FileChangePayload): void {
    this.postMessage({ type: "fileChange", payload });
  }

  private toggleToolCard(id: string): void {
    const card = this.toolCards.get(id);
    if (card) {
      card.collapsed = !card.collapsed;
      this.postMessage({ type: "toggleToolCard", id, collapsed: card.collapsed });
    }
  }

  private async openFile(path: string, line?: number): Promise<void> {
    const uri = vscode.Uri.file(path);
    try {
      const doc = await vscode.workspace.openTextDocument(uri);
      const editor = await vscode.window.showTextDocument(doc, {
        preview: true,
        preserveFocus: true,
      });
      if (line !== undefined && line >= 0) {
        const pos = new vscode.Position(line, 0);
        editor.selection = new vscode.Selection(pos, pos);
        editor.revealRange(new vscode.Range(pos, pos), vscode.TextEditorRevealType.InCenter);
      }
    } catch {
      // File may not exist or be inaccessible
    }
  }

  private addMessage(role: ChatMessageRole, text: string): void {
    const msg: ChatMessage = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      role,
      text,
      timestamp: Date.now(),
    };
    this.messages.push(msg);
    this.postMessage({ type: "newMessage", message: msg });
  }

  /** Append text to the last assistant message (streaming). */
  private appendAssistantText(chunk: string): void {
    const last = this.messages[this.messages.length - 1];
    if (last && last.role === "assistant") {
      last.text += chunk;
      this.postMessage({ type: "appendText", id: last.id, chunk });
    } else {
      this.addMessage("assistant", chunk);
    }
  }

  private postMessage(msg: any): void {
    this.view?.webview.postMessage(msg);
  }

  // ── HTML ───────────────────────────────────────────────────────

  private getHtmlForWebview(webview: vscode.Webview): string {
    const nonce = getNonce();

    return /*html*/ `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>CrabCode Chat</title>
  <style nonce="${nonce}">
    :root { --font: var(--vscode-font-family); }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: var(--font);
      font-size: var(--vscode-font-size);
      color: var(--vscode-foreground);
      background: var(--vscode-sideBar-background);
      display: flex; flex-direction: column; height: 100vh;
    }
    #messages {
      flex: 1; overflow-y: auto; padding: 8px;
    }
    .msg { margin-bottom: 8px; padding: 6px 8px; border-radius: 6px; }
    .msg.user { background: var(--vscode-input-background); }
    .msg.assistant { background: var(--vscode-editor-background); }
    .msg.system { background: var(--vscode-editorWarning-background, #553300); opacity: 0.85; }
    .msg .role { font-weight: bold; font-size: 0.85em; margin-bottom: 2px; }
    .msg .text { white-space: pre-wrap; word-break: break-word; }

    /* Tool cards */
    .tool-card {
      margin: 6px 0; border-radius: 6px;
      border: 1px solid var(--vscode-panel-border, var(--vscode-editorWidget-border, #333));
      overflow: hidden; font-size: 0.9em;
    }
    .tool-card-header {
      display: flex; align-items: center; gap: 6px;
      padding: 4px 8px; cursor: pointer;
      background: var(--vscode-list-hoverBackground, rgba(255,255,255,0.04));
    }
    .tool-card-header .icon { font-size: 1em; opacity: 0.7; }
    .tool-card-header .tool-name { font-weight: 600; flex: 1; }
    .tool-card-header .chevron { opacity: 0.5; transition: transform 0.15s; }
    .tool-card-header .chevron.collapsed { transform: rotate(-90deg); }
    .tool-card-header .status { font-size: 0.8em; opacity: 0.6; }
    .tool-card-header .status.error { color: var(--vscode-errorForeground, #f48771); }
    .tool-card-header .status.ok { color: var(--vscode-terminal-ansiGreen, #89d185); }
    .tool-card-body {
      padding: 6px 8px; max-height: 200px; overflow-y: auto;
      background: var(--vscode-editor-background);
      border-top: 1px solid var(--vscode-panel-border, var(--vscode-editorWidget-border, #333));
    }
    .tool-card-body.hidden { display: none; }
    .tool-card-body pre {
      white-space: pre-wrap; word-break: break-word;
      font-family: var(--vscode-editor-font-family, monospace);
      font-size: 0.9em;
    }

    /* Diff display inside tool cards */
    .diff-line-add { color: var(--vscode-terminal-ansiGreen, #89d185); }
    .diff-line-del { color: var(--vscode-terminal-ansiRed, #f48771); }
    .diff-line-ctx { color: var(--vscode-descriptionForeground, #888); }

    /* File change pill */
    .file-change {
      display: inline-flex; align-items: center; gap: 4px;
      padding: 2px 8px; margin: 2px 0; border-radius: 10px;
      font-size: 0.8em; background: var(--vscode-input-background);
    }
    .file-change .action {
      font-weight: 600; text-transform: uppercase; font-size: 0.85em;
    }
    .file-change .action.create { color: var(--vscode-terminal-ansiGreen, #89d185); }
    .file-change .action.modify { color: var(--vscode-terminal-ansiYellow, #cca700); }
    .file-change .action.delete { color: var(--vscode-terminal-ansiRed, #f48771); }
    .file-change .path {
      cursor: pointer; text-decoration: underline;
      text-underline-offset: 2px;
    }

    #input-area {
      display: flex; padding: 6px;
      border-top: 1px solid var(--vscode-panel-border, var(--vscode-editorWidget-border, #333));
    }
    #input {
      flex: 1; resize: none; border: none; outline: none;
      background: var(--vscode-input-background);
      color: var(--vscode-input-foreground);
      padding: 6px 8px; border-radius: 4px;
      font-family: var(--font); font-size: var(--vscode-font-size);
    }
    #send-btn {
      margin-left: 6px; padding: 6px 12px;
      background: var(--vscode-button-background);
      color: var(--vscode-button-foreground);
      border: none; border-radius: 4px; cursor: pointer;
    }
    #send-btn:hover { background: var(--vscode-button-hoverBackground); }
  </style>
</head>
<body>
  <div id="messages"></div>
  <div id="input-area">
    <textarea id="input" rows="2" placeholder="Ask CrabCode…"></textarea>
    <button id="send-btn">Send</button>
  </div>
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    const msgContainer = document.getElementById('messages');
    const input = document.getElementById('input');
    const sendBtn = document.getElementById('send-btn');

    // ── Tool card state ──────────────────────────────────────────
    const toolCards = new Map();

    function addMessageEl(msg) {
      const div = document.createElement('div');
      div.className = 'msg ' + msg.role;
      div.id = 'msg-' + msg.id;
      div.innerHTML = '<div class="role">' + capitalize(msg.role) + '</div><div class="text">' + escapeHtml(msg.text) + '</div>';
      msgContainer.appendChild(div);
      msgContainer.scrollTop = msgContainer.scrollHeight;
    }

    function renderToolCard(card) {
      const existing = document.getElementById('tool-' + card.id);
      if (existing) { updateToolCard(existing, card); return; }

      const el = document.createElement('div');
      el.className = 'tool-card';
      el.id = 'tool-' + card.id;
      el.innerHTML = buildToolCardHtml(card);
      msgContainer.appendChild(el);
      msgContainer.scrollTop = msgContainer.scrollHeight;

      el.querySelector('.tool-card-header').addEventListener('click', () => {
        vscode.postMessage({ type: 'toggleToolCard', id: card.id });
      });

      el.querySelector('.path')?.addEventListener('click', (e) => {
        const path = e.target.dataset.path;
        if (path) vscode.postMessage({ type: 'openFile', path });
      });

      toolCards.set(card.id, card);
    }

    function updateToolCard(el, card) {
      el.innerHTML = buildToolCardHtml(card);
      el.querySelector('.tool-card-header').addEventListener('click', () => {
        vscode.postMessage({ type: 'toggleToolCard', id: card.id });
      });
      el.querySelector('.path')?.addEventListener('click', (e) => {
        const path = e.target.dataset.path;
        if (path) vscode.postMessage({ type: 'openFile', path });
      });
      toolCards.set(card.id, card);
    }

    function buildToolCardHtml(card) {
      const chevron = card.collapsed ? 'chevron collapsed' : 'chevron';
      let statusHtml = '';
      if (card.result !== null) {
        statusHtml = card.isError
          ? '<span class="status error">error</span>'
          : '<span class="status ok">done</span>';
      } else {
        statusHtml = '<span class="status">running…</span>';
      }

      const inputStr = formatToolInput(card.toolName, card.input);
      let bodyHtml = '';
      if (!card.collapsed) {
        if (card.result !== null) {
          bodyHtml = '<div class="tool-card-body"><pre>' + renderResult(card.result, card.toolName) + '</pre></div>';
        } else {
          bodyHtml = '<div class="tool-card-body"><pre>' + escapeHtml(inputStr) + '</pre></div>';
        }
      } else if (card.result !== null) {
        // Show a one-line preview when collapsed
        const preview = card.result.split('\\n')[0].substring(0, 120);
        statusHtml += ' <span style="opacity:0.5">' + escapeHtml(preview) + (card.result.length > 120 ? '…' : '') + '</span>';
      }

      return '<div class="tool-card-header">' +
        '<span class="icon">⚙</span>' +
        '<span class="tool-name">' + escapeHtml(card.toolName) + '</span>' +
        statusHtml +
        '<span class="' + chevron + '">▾</span>' +
        '</div>' + bodyHtml;
    }

    function formatToolInput(toolName, input) {
      // Show concise input for common tools
      if (input.file_path || input.path) {
        const p = input.file_path || input.path;
        const rest = Object.entries(input).filter(([k]) => k !== 'file_path' && k !== 'path')
          .map(([k,v]) => k + ': ' + (typeof v === 'string' ? v.substring(0,80) : JSON.stringify(v)))
          .join(', ');
        return p + (rest ? '\\n' + rest : '');
      }
      return JSON.stringify(input, null, 2);
    }

    function renderResult(text, toolName) {
      // If it looks like a diff, colorize lines
      if (text.startsWith('---') || text.startsWith('diff --git') || text.includes('\\n+++')) {
        return text.split('\\n').map(line => {
          if (line.startsWith('+++') || line.startsWith('+')) return '<span class="diff-line-add">' + escapeHtml(line) + '</span>';
          if (line.startsWith('---') || line.startsWith('-')) return '<span class="diff-line-del">' + escapeHtml(line) + '</span>';
          if (line.startsWith('@@')) return '<span class="diff-line-ctx">' + escapeHtml(line) + '</span>';
          return escapeHtml(line);
        }).join('\\n');
      }
      return escapeHtml(text);
    }

    function addFileChangePill(payload) {
      const div = document.createElement('div');
      div.className = 'file-change';
      const actionClass = payload.action; // create | modify | delete
      const shortPath = payload.path.split('/').pop() || payload.path;
      div.innerHTML =
        '<span class="action ' + actionClass + '">' + escapeHtml(payload.action) + '</span>' +
        '<span class="path" data-path="' + escapeHtml(payload.path) + '" title="' + escapeHtml(payload.path) + '">' + escapeHtml(shortPath) + '</span>';
      msgContainer.appendChild(div);
      msgContainer.scrollTop = msgContainer.scrollHeight;

      div.querySelector('.path').addEventListener('click', () => {
        vscode.postMessage({ type: 'openFile', path: payload.path });
      });
    }

    function capitalize(s) { return s.charAt(0).toUpperCase() + s.slice(1); }
    function escapeHtml(t) {
      if (t == null) return '';
      return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }

    function send() {
      const text = input.value.trim();
      if (!text) return;
      vscode.postMessage({ type: 'sendMessage', text });
      input.value = '';
    }

    sendBtn.addEventListener('click', send);
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
    });

    window.addEventListener('message', event => {
      const msg = event.data;
      if (!msg) return;
      switch (msg.type) {
        case 'newMessage':
          addMessageEl(msg.message);
          break;
        case 'appendText': {
          const el = document.getElementById('msg-' + msg.id);
          if (el) {
            const textEl = el.querySelector('.text');
            textEl.textContent += msg.chunk;
            msgContainer.scrollTop = msgContainer.scrollHeight;
          }
          break;
        }
        case 'history':
          msgContainer.innerHTML = '';
          msg.messages.forEach(addMessageEl);
          break;
        case 'prefill':
          input.value = msg.text;
          input.focus();
          break;
        case 'toolUse':
          renderToolCard(msg.card);
          break;
        case 'toolResult':
          renderToolCard(msg.card);
          break;
        case 'toggleToolCard': {
          const card = toolCards.get(msg.id);
          if (card) {
            card.collapsed = msg.collapsed;
            const el = document.getElementById('tool-' + msg.id);
            if (el) updateToolCard(el, card);
          }
          break;
        }
        case 'fileChange':
          addFileChangePill(msg.payload);
          break;
      }
    });

    vscode.postMessage({ type: 'requestHistory' });
  </script>
</body>
</html>`;
  }
}

// ── Helpers ───────────────────────────────────────────────────────

function getNonce(): string {
  let text = "";
  const possible = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  for (let i = 0; i < 32; i++) {
    text += possible.charAt(Math.floor(Math.random() * possible.length));
  }
  return text;
}
