import * as path from "path";
import * as vscode from "vscode";
import { Buffer } from "buffer";

import type { CrabCodeConnection } from "./connection";
import type {
  PendingEditActionMessage,
  PendingEditReviewSummary,
} from "./chatPanel";
import type {
  EventPayload,
  ToolResultPayload,
  ToolUsePayload,
} from "./client/types";

type PendingEditAction = "create" | "modify";
type LineOpKind = "equal" | "delete" | "insert";

interface ToolSnapshot {
  toolUseId: string;
  filePath: string;
  beforeExists: boolean;
  beforeContent: string;
}

interface PendingHunk {
  id: string;
  oldStartLine: number;
  oldLineCount: number;
  newStartLine: number;
  newLineCount: number;
  oldLines: string[];
  newLines: string[];
}

interface PendingChange {
  id: string;
  filePath: string;
  shortPath: string;
  action: PendingEditAction;
  beforeExists: boolean;
  beforeContent: string;
  afterContent: string;
  stats: DiffStats;
  hunks: PendingHunk[];
  toolUseIds: Set<string>;
  updatedAt: number;
}

interface DiffStats {
  added: number;
  removed: number;
}

interface LineOp {
  type: LineOpKind;
  line: string;
}

interface DiffResult {
  hunks: PendingHunk[];
  stats: DiffStats;
}

interface ReviewState {
  files: Array<{
    id: string;
    shortPath: string;
    path: string;
    action: PendingEditAction;
    added: number;
    removed: number;
    hunkCount: number;
  }>;
  currentIndex: number;
  totalFiles: number;
  file: {
    id: string;
    shortPath: string;
    path: string;
    action: PendingEditAction;
    added: number;
    removed: number;
    hunks: Array<PendingHunk & { index: number; total: number }>;
  } | null;
}

interface TextRead {
  exists: boolean;
  content: string;
}

const VIRTUAL_DIFF_SCHEME = "crabcode-pending-edit";
const MAX_LCS_CELLS = 8_000_000;
const CONTEXT_LINES = 3;
const REVIEW_REVEAL_DELAY_MS = 120;

export class PendingEditManager implements vscode.Disposable, vscode.CodeLensProvider {
  private readonly toolSnapshots = new Map<string, ToolSnapshot>();
  private readonly changes = new Map<string, PendingChange>();
  private readonly changeByPath = new Map<string, string>();
  private readonly disposables: vscode.Disposable[] = [];
  private readonly codeLensEmitter = new vscode.EventEmitter<void>();
  private readonly contentEmitter = new vscode.EventEmitter<vscode.Uri>();
  private readonly virtualDocuments = new Map<string, string>();
  private reviewPanel: vscode.WebviewPanel | undefined;
  private activeReviewChangeId: string | null = null;
  private readonly decorationType = vscode.window.createTextEditorDecorationType({
    isWholeLine: true,
    backgroundColor: "rgba(46, 160, 67, 0.22)",
    overviewRulerColor: "rgba(46, 160, 67, 0.85)",
    overviewRulerLane: vscode.OverviewRulerLane.Right,
  });

  public readonly onDidChangeCodeLenses = this.codeLensEmitter.event;

  constructor(
    connection: CrabCodeConnection,
    private readonly chatPanel: {
      setPendingEditReview(summary: PendingEditReviewSummary | null): void;
      onPendingEditAction: vscode.Event<PendingEditActionMessage>;
    },
  ) {
    this.disposables.push(
      connection.on("message", (payload: EventPayload) => {
        void this.handleServerEvent(payload);
      }),
      chatPanel.onPendingEditAction((msg) => {
        void this.handleChatAction(msg);
      }),
      vscode.languages.registerCodeLensProvider(
        [{ scheme: "file" }, { scheme: VIRTUAL_DIFF_SCHEME }],
        this,
      ),
      vscode.workspace.registerTextDocumentContentProvider(
        VIRTUAL_DIFF_SCHEME,
        {
          onDidChange: this.contentEmitter.event,
          provideTextDocumentContent: (uri) => this.virtualDocuments.get(uri.toString()) ?? "",
        },
      ),
      vscode.window.onDidChangeVisibleTextEditors(() => {
        this.refreshDecorations();
      }),
      vscode.workspace.onDidChangeTextDocument((event) => {
        const filePath = normalizeFilePath(event.document.uri.fsPath);
        if (this.changeByPath.has(filePath)) {
          this.refreshDecorations();
          this.codeLensEmitter.fire();
        }
      }),
      vscode.commands.registerCommand("crabcode.pendingEdits.undoFile", (changeId: string) => {
        void this.undoFile(changeId);
      }),
      vscode.commands.registerCommand("crabcode.pendingEdits.keepFile", (changeId: string) => {
        void this.keepFile(changeId);
      }),
      vscode.commands.registerCommand("crabcode.pendingEdits.reviewFile", (changeId: string) => {
        void this.reviewFile(changeId);
      }),
      vscode.commands.registerCommand(
        "crabcode.pendingEdits.previousFile",
        (changeId: string) => {
          void this.reviewFileByOffset(changeId, -1);
        },
      ),
      vscode.commands.registerCommand(
        "crabcode.pendingEdits.nextFile",
        (changeId: string) => {
          void this.reviewFileByOffset(changeId, 1);
        },
      ),
      vscode.commands.registerCommand(
        "crabcode.pendingEdits.undoHunk",
        (changeId: string, hunkId: string) => {
          void this.undoHunk(changeId, hunkId);
        },
      ),
      vscode.commands.registerCommand(
        "crabcode.pendingEdits.keepHunk",
        (changeId: string, hunkId: string) => {
          void this.keepHunk(changeId, hunkId);
        },
      ),
      vscode.commands.registerCommand(
        "crabcode.pendingEdits.previousHunk",
        (changeId: string, hunkId: string) => {
          void this.revealHunkByOffset(changeId, hunkId, -1);
        },
      ),
      vscode.commands.registerCommand(
        "crabcode.pendingEdits.nextHunk",
        (changeId: string, hunkId: string) => {
          void this.revealHunkByOffset(changeId, hunkId, 1);
        },
      ),
      vscode.commands.registerCommand("crabcode.pendingEdits.undoAll", () => {
        void this.undoAll();
      }),
      vscode.commands.registerCommand("crabcode.pendingEdits.keepAll", () => {
        void this.keepAll();
      }),
      vscode.commands.registerCommand("crabcode.pendingEdits.reviewAll", () => {
        void this.reviewAll();
      }),
      vscode.commands.registerCommand("crabcode.pendingEdits.noop", () => undefined),
    );
  }

  dispose(): void {
    for (const disposable of this.disposables) {
      disposable.dispose();
    }
    this.disposables.length = 0;
    this.toolSnapshots.clear();
    this.changes.clear();
    this.changeByPath.clear();
    this.virtualDocuments.clear();
    this.reviewPanel?.dispose();
    this.reviewPanel = undefined;
    this.decorationType.dispose();
    this.codeLensEmitter.dispose();
    this.contentEmitter.dispose();
    this.chatPanel.setPendingEditReview(null);
  }

  provideCodeLenses(document: vscode.TextDocument): vscode.CodeLens[] {
    const lensTarget = this.resolveLensTarget(document);
    const change = lensTarget?.change;
    if (!change) {
      return [];
    }

    const top = new vscode.Range(new vscode.Position(0, 0), new vscode.Position(0, 0));
    const changes = this.sortedChanges();
    const fileIndex = Math.max(0, changes.findIndex((candidate) => candidate.id === change.id));
    const canMoveFiles = changes.length > 1;
    const lenses = [
      new vscode.CodeLens(top, {
        title: canMoveFiles ? "‹ File" : "‹",
        command: canMoveFiles ? "crabcode.pendingEdits.previousFile" : "crabcode.pendingEdits.noop",
        arguments: [change.id],
      }),
      new vscode.CodeLens(top, {
        title: `${fileIndex + 1} of ${Math.max(1, changes.length)}`,
        command: "crabcode.pendingEdits.noop",
      }),
      new vscode.CodeLens(top, {
        title: canMoveFiles ? "File ›" : "›",
        command: canMoveFiles ? "crabcode.pendingEdits.nextFile" : "crabcode.pendingEdits.noop",
        arguments: [change.id],
      }),
      new vscode.CodeLens(top, {
        title: "Undo File",
        command: "crabcode.pendingEdits.undoFile",
        arguments: [change.id],
      }),
      new vscode.CodeLens(top, {
        title: "Keep File",
        command: "crabcode.pendingEdits.keepFile",
        arguments: [change.id],
      }),
      new vscode.CodeLens(top, {
        title: "Review File",
        command: "crabcode.pendingEdits.reviewFile",
        arguments: [change.id],
      }),
    ];

    for (let index = 0; index < change.hunks.length; index++) {
      const hunk = change.hunks[index];
      const range = rangeForHunk(document, hunk);
      const hasManyHunks = change.hunks.length > 1;
      lenses.push(
        new vscode.CodeLens(range, {
          title: canMoveFiles ? "‹" : "‹",
          command: canMoveFiles ? "crabcode.pendingEdits.previousFile" : "crabcode.pendingEdits.noop",
          arguments: [change.id],
        }),
        new vscode.CodeLens(range, {
          title: `${fileIndex + 1} of ${Math.max(1, changes.length)}`,
          command: "crabcode.pendingEdits.noop",
        }),
        new vscode.CodeLens(range, {
          title: canMoveFiles ? "›" : "›",
          command: canMoveFiles ? "crabcode.pendingEdits.nextFile" : "crabcode.pendingEdits.noop",
          arguments: [change.id],
        }),
        new vscode.CodeLens(range, {
          title: "Undo File",
          command: "crabcode.pendingEdits.undoFile",
          arguments: [change.id],
        }),
        new vscode.CodeLens(range, {
          title: "Keep File",
          command: "crabcode.pendingEdits.keepFile",
          arguments: [change.id],
        }),
        new vscode.CodeLens(range, {
          title: hasManyHunks ? "‹ Block" : "‹",
          command: hasManyHunks ? "crabcode.pendingEdits.previousHunk" : "crabcode.pendingEdits.noop",
          arguments: [change.id, hunk.id],
        }),
        new vscode.CodeLens(range, {
          title: `Block ${index + 1} of ${change.hunks.length}`,
          command: "crabcode.pendingEdits.noop",
        }),
        new vscode.CodeLens(range, {
          title: hasManyHunks ? "Block ›" : "›",
          command: hasManyHunks ? "crabcode.pendingEdits.nextHunk" : "crabcode.pendingEdits.noop",
          arguments: [change.id, hunk.id],
        }),
        new vscode.CodeLens(range, {
          title: "Undo Block",
          command: "crabcode.pendingEdits.undoHunk",
          arguments: [change.id, hunk.id],
        }),
        new vscode.CodeLens(range, {
          title: "Keep Block",
          command: "crabcode.pendingEdits.keepHunk",
          arguments: [change.id, hunk.id],
        }),
      );
    }

    return lenses;
  }

  private async handleServerEvent(payload: EventPayload): Promise<void> {
    switch (payload.type) {
      case "tool_use":
        await this.captureToolUse(payload as ToolUsePayload);
        break;
      case "tool_result":
        await this.finalizeToolResult(payload as ToolResultPayload);
        break;
    }
  }

  private async captureToolUse(payload: ToolUsePayload): Promise<void> {
    if (!isEditOrWriteTool(payload.tool_name)) {
      return;
    }
    const filePath = this.resolveToolFilePath(payload.tool_input);
    if (!filePath) {
      return;
    }
    const before = await readTextFile(filePath);
    this.toolSnapshots.set(payload.tool_use_id, {
      toolUseId: payload.tool_use_id,
      filePath,
      beforeExists: before.exists,
      beforeContent: before.content,
    });
  }

  private async finalizeToolResult(payload: ToolResultPayload): Promise<void> {
    const snapshot = this.toolSnapshots.get(payload.tool_use_id);
    if (!snapshot) {
      return;
    }
    this.toolSnapshots.delete(payload.tool_use_id);

    if (payload.is_error) {
      return;
    }

    const after = await readTextFile(snapshot.filePath);
    if (!after.exists) {
      return;
    }

    const existing = this.getChangeForPath(snapshot.filePath);
    const beforeExists = existing?.beforeExists ?? snapshot.beforeExists;
    const beforeContent = existing?.beforeContent ?? (snapshot.beforeExists ? snapshot.beforeContent : "");
    const action: PendingEditAction = beforeExists ? "modify" : "create";
    const diff = computeLineDiff(beforeContent, after.content);

    if (beforeExists && diff.hunks.length === 0) {
      if (existing) {
        this.removeChange(existing.id);
      }
      return;
    }

    const change: PendingChange = existing ?? {
      id: `edit-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      filePath: snapshot.filePath,
      shortPath: shortFilePath(snapshot.filePath),
      action,
      beforeExists,
      beforeContent,
      afterContent: after.content,
      stats: diff.stats,
      hunks: diff.hunks,
      toolUseIds: new Set<string>(),
      updatedAt: Date.now(),
    };

    change.action = action;
    change.beforeExists = beforeExists;
    change.beforeContent = beforeContent;
    change.afterContent = after.content;
    change.stats = diff.stats;
    change.hunks = diff.hunks;
    change.toolUseIds.add(snapshot.toolUseId);
    change.updatedAt = Date.now();

    this.changes.set(change.id, change);
    this.changeByPath.set(normalizeFilePath(change.filePath), change.id);
    this.syncVirtualDocuments(change);
    this.updatePresentation();
    await this.reviewFile(change.id);
    vscode.window.setStatusBarMessage(`CrabCode：待确认 ${change.shortPath}`, 3000);
  }

  private resolveToolFilePath(input: Record<string, unknown>): string | null {
    const raw = firstString(input.file_path, input.path);
    if (!raw) {
      return null;
    }
    if (path.isAbsolute(raw)) {
      return normalizeFilePath(raw);
    }
    const cwd = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    return normalizeFilePath(cwd ? path.resolve(cwd, raw) : path.resolve(raw));
  }

  private async handleChatAction(msg: PendingEditActionMessage): Promise<void> {
    switch (msg.action) {
      case "undoAll":
        await this.undoAll();
        break;
      case "keepAll":
        await this.keepAll();
        break;
      case "reviewAll":
        await this.reviewAll();
        break;
      case "undoFile":
        if (msg.changeId) await this.undoFile(msg.changeId);
        break;
      case "keepFile":
        if (msg.changeId) await this.keepFile(msg.changeId);
        break;
      case "reviewFile":
        if (msg.changeId) await this.reviewFile(msg.changeId);
        break;
      case "undoHunk":
        if (msg.changeId && msg.hunkId) await this.undoHunk(msg.changeId, msg.hunkId);
        break;
      case "keepHunk":
        if (msg.changeId && msg.hunkId) await this.keepHunk(msg.changeId, msg.hunkId);
        break;
    }
  }

  private async undoAll(): Promise<void> {
    for (const id of [...this.changes.keys()]) {
      await this.undoFile(id, false);
    }
    this.updatePresentation();
  }

  private async keepAll(): Promise<void> {
    for (const id of [...this.changes.keys()]) {
      this.keepFile(id, false);
    }
    this.updatePresentation();
  }

  private async reviewAll(): Promise<void> {
    const changes = this.sortedChanges();
    if (changes.length === 0) {
      return;
    }
    if (changes.length === 1) {
      await this.reviewFile(changes[0].id);
      return;
    }

    const picked = await vscode.window.showQuickPick(
      changes.map((change) => ({
        label: change.shortPath,
        description: change.action === "create" ? "new file" : `${formatStats(change.stats)}`,
        changeId: change.id,
      })),
      { title: "CrabCode：选择要查看的文件 diff" },
    );
    if (picked) {
      await this.reviewFile(picked.changeId);
    }
  }

  private async undoFile(changeId: string, update = true): Promise<void> {
    const change = this.changes.get(changeId);
    if (!change) {
      return;
    }
    if (!(await this.confirmIfDrifted(change, "撤销"))) {
      return;
    }

    if (change.beforeExists) {
      await writeTextFile(change.filePath, change.beforeContent);
      await this.revealFile(change.filePath);
    } else {
      await deleteFileIfExists(change.filePath);
    }

    this.removeChange(change.id, false);
    if (update) {
      this.updatePresentation();
    }
    vscode.window.setStatusBarMessage(`CrabCode：已撤销 ${change.shortPath}`, 3000);
  }

  private keepFile(changeId: string, update = true): void {
    const change = this.changes.get(changeId);
    if (!change) {
      return;
    }
    this.removeChange(change.id, false);
    if (update) {
      this.updatePresentation();
    }
    vscode.window.setStatusBarMessage(`CrabCode：已保留 ${change.shortPath}`, 3000);
  }

  private async reviewFile(changeId: string): Promise<void> {
    const change = this.changes.get(changeId);
    if (!change) {
      return;
    }

    this.activeReviewChangeId = change.id;
    this.ensureReviewPanel();
    this.reviewPanel?.reveal(vscode.ViewColumn.Beside, false);
    this.postReviewState();
  }

  private async reviewFileNative(changeId: string): Promise<void> {
    const change = this.changes.get(changeId);
    if (!change) {
      return;
    }

    const basename = path.basename(change.filePath);
    const beforeUri = vscode.Uri.from({
      scheme: VIRTUAL_DIFF_SCHEME,
      path: `/${change.id}/before/${basename}`,
      query: `changeId=${encodeURIComponent(change.id)}&side=before`,
    });
    const afterUri = vscode.Uri.from({
      scheme: VIRTUAL_DIFF_SCHEME,
      path: `/${change.id}/after/${basename}`,
      query: `changeId=${encodeURIComponent(change.id)}&side=after`,
    });
    this.virtualDocuments.set(beforeUri.toString(), change.beforeContent);
    this.virtualDocuments.set(afterUri.toString(), change.afterContent);
    this.contentEmitter.fire(beforeUri);
    this.contentEmitter.fire(afterUri);

    await vscode.commands.executeCommand(
      "vscode.diff",
      beforeUri,
      afterUri,
      `CrabCode Review: ${change.shortPath}`,
      { preview: true },
    );
  }

  private async reviewFileByOffset(changeId: string, offset: number): Promise<void> {
    const changes = this.sortedChanges();
    if (changes.length === 0) {
      return;
    }
    const currentIndex = changes.findIndex((change) => change.id === changeId);
    const startIndex = currentIndex >= 0 ? currentIndex : 0;
    const nextIndex = (startIndex + offset + changes.length) % changes.length;
    await this.reviewFile(changes[nextIndex].id);
  }

  private ensureReviewPanel(): void {
    if (this.reviewPanel) {
      return;
    }

    const panel = vscode.window.createWebviewPanel(
      "crabcode.pendingEditReview",
      "CrabCode Review",
      vscode.ViewColumn.Beside,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
      },
    );
    this.reviewPanel = panel;
    panel.webview.html = this.getReviewHtml(panel.webview);
    panel.webview.onDidReceiveMessage((msg: any) => {
      void this.handleReviewMessage(msg);
    });
    panel.onDidDispose(() => {
      this.reviewPanel = undefined;
    });
  }

  private async handleReviewMessage(msg: any): Promise<void> {
    const action = typeof msg?.action === "string" ? msg.action : "";
    const changeId = typeof msg?.changeId === "string" ? msg.changeId : this.activeReviewChangeId ?? "";
    const hunkId = typeof msg?.hunkId === "string" ? msg.hunkId : "";

    switch (msg?.type) {
      case "ready":
        this.postReviewState();
        return;
      case "selectFile":
        if (this.changes.has(changeId)) {
          this.activeReviewChangeId = changeId;
          this.postReviewState();
        }
        return;
      case "moveFile":
        await this.reviewFileByOffset(changeId, Number(msg.offset) || 0);
        return;
      case "openNativeDiff":
        await this.reviewFileNative(changeId);
        return;
      case "openFile": {
        const change = this.changes.get(changeId);
        if (change) {
          await this.revealChange(change);
        }
        return;
      }
      case "reviewAction":
        break;
      default:
        return;
    }

    switch (action) {
      case "undoFile":
        await this.undoFile(changeId);
        break;
      case "keepFile":
        await this.keepFile(changeId);
        break;
      case "undoHunk":
        if (hunkId) await this.undoHunk(changeId, hunkId);
        break;
      case "keepHunk":
        if (hunkId) await this.keepHunk(changeId, hunkId);
        break;
    }
    this.ensureActiveReviewChange();
    this.postReviewState();
  }

  private postReviewState(): void {
    if (!this.reviewPanel) {
      return;
    }
    void this.reviewPanel.webview.postMessage({
      type: "reviewState",
      state: this.buildReviewState(),
    });
  }

  private ensureActiveReviewChange(): void {
    if (this.activeReviewChangeId && this.changes.has(this.activeReviewChangeId)) {
      return;
    }
    this.activeReviewChangeId = this.sortedChanges()[0]?.id ?? null;
  }

  private buildReviewState(): ReviewState {
    const changes = this.sortedChanges();
    this.ensureActiveReviewChange();
    const activeId = this.activeReviewChangeId;
    const currentIndex = Math.max(0, changes.findIndex((change) => change.id === activeId));
    const current = changes[currentIndex] ?? null;
    return {
      totalFiles: changes.length,
      currentIndex,
      files: changes.map((change) => ({
        id: change.id,
        shortPath: change.shortPath,
        path: change.filePath,
        action: change.action,
        added: change.stats.added,
        removed: change.stats.removed,
        hunkCount: change.hunks.length,
      })),
      file: current
        ? {
            id: current.id,
            shortPath: current.shortPath,
            path: current.filePath,
            action: current.action,
            added: current.stats.added,
            removed: current.stats.removed,
            hunks: current.hunks.map((hunk, index) => ({
              ...hunk,
              index,
              total: current.hunks.length,
            })),
          }
        : null,
    };
  }

  private async revealHunkByOffset(changeId: string, hunkId: string, offset: number): Promise<void> {
    const change = this.changes.get(changeId);
    if (!change || change.hunks.length === 0) {
      return;
    }
    const currentIndex = change.hunks.findIndex((hunk) => hunk.id === hunkId);
    const startIndex = currentIndex >= 0 ? currentIndex : 0;
    const nextIndex = (startIndex + offset + change.hunks.length) % change.hunks.length;
    await this.revealReviewedHunk(change, change.hunks[nextIndex]);
  }

  private async undoHunk(changeId: string, hunkId: string): Promise<void> {
    const change = this.changes.get(changeId);
    const hunk = change?.hunks.find((candidate) => candidate.id === hunkId);
    if (!change || !hunk) {
      return;
    }

    const current = await readTextFile(change.filePath);
    if (!current.exists) {
      vscode.window.showWarningMessage(`CrabCode：无法撤销，文件不存在：${change.shortPath}`);
      return;
    }

    const eol = detectEol(current.content);
    const lines = splitLines(current.content);
    const start = findHunkStart(lines, hunk);
    if (start < 0) {
      vscode.window.showWarningMessage(
        `CrabCode：${change.shortPath} 已被继续修改，无法精确撤销这个 block。`,
      );
      return;
    }

    lines.splice(start, hunk.newLineCount, ...hunk.oldLines);
    const nextContent = joinLines(lines, eol, hasFinalNewline(current.content));
    await writeTextFile(change.filePath, nextContent);
    change.afterContent = nextContent;
    this.recomputeChange(change);
    this.updatePresentation();
    await this.revealChange(change, true);
  }

  private async keepHunk(changeId: string, hunkId: string): Promise<void> {
    const change = this.changes.get(changeId);
    const hunk = change?.hunks.find((candidate) => candidate.id === hunkId);
    if (!change || !hunk) {
      return;
    }

    change.beforeContent = applyHunkToBase(change.beforeContent, hunk, change.afterContent);
    const current = await readTextFile(change.filePath);
    if (current.exists) {
      change.afterContent = current.content;
    }
    this.recomputeChange(change);
    this.updatePresentation();
  }

  private async confirmIfDrifted(change: PendingChange, verb: string): Promise<boolean> {
    const current = await readTextFile(change.filePath);
    const currentContent = current.exists ? current.content : "";
    if (current.exists === true && currentContent === change.afterContent) {
      return true;
    }
    if (!current.exists && !change.beforeExists) {
      return true;
    }
    const choice = await vscode.window.showWarningMessage(
      `CrabCode：${change.shortPath} 在生成后又被修改过，仍然${verb}会覆盖这些改动。`,
      { modal: true },
      `仍然${verb}`,
      "取消",
    );
    return choice === `仍然${verb}`;
  }

  private recomputeChange(change: PendingChange): void {
    const diff = computeLineDiff(change.beforeContent, change.afterContent);
    change.hunks = diff.hunks;
    change.stats = diff.stats;
    change.updatedAt = Date.now();

    if (change.hunks.length === 0) {
      this.removeChange(change.id, false);
      return;
    }
    if (!change.beforeExists) {
      change.action = "create";
    }
    this.changes.set(change.id, change);
    this.changeByPath.set(normalizeFilePath(change.filePath), change.id);
    this.syncVirtualDocuments(change);
  }

  private removeChange(changeId: string, update = true): void {
    const change = this.changes.get(changeId);
    if (change) {
      this.changeByPath.delete(normalizeFilePath(change.filePath));
    }
    this.changes.delete(changeId);
    if (this.activeReviewChangeId === changeId) {
      this.activeReviewChangeId = null;
      this.ensureActiveReviewChange();
    }
    if (update) {
      this.updatePresentation();
    }
  }

  private getChangeForPath(filePath: string): PendingChange | undefined {
    const id = this.changeByPath.get(normalizeFilePath(filePath));
    return id ? this.changes.get(id) : undefined;
  }

  private updatePresentation(): void {
    this.chatPanel.setPendingEditReview(this.buildReviewSummary());
    this.refreshDecorations();
    this.codeLensEmitter.fire();
    this.postReviewState();
  }

  private buildReviewSummary(): PendingEditReviewSummary | null {
    const changes = this.sortedChanges();
    if (changes.length === 0) {
      return null;
    }
    return {
      totalFiles: changes.length,
      totalHunks: changes.reduce((sum, change) => sum + change.hunks.length, 0),
      files: changes.map((change) => ({
        id: change.id,
        path: change.filePath,
        shortPath: change.shortPath,
        action: change.action,
        added: change.stats.added,
        removed: change.stats.removed,
        hunkCount: change.hunks.length,
      })),
    };
  }

  private refreshDecorations(): void {
    for (const editor of vscode.window.visibleTextEditors) {
      const change = editor.document.uri.scheme === "file"
        ? this.getChangeForPath(editor.document.uri.fsPath)
        : undefined;
      const ranges = change
        ? change.hunks.map((hunk) => rangeForHunk(editor.document, hunk))
        : [];
      editor.setDecorations(this.decorationType, ranges);
    }
  }

  private async revealChange(change: PendingChange, preserveFocus = false): Promise<void> {
    if (change.hunks.length === 0) {
      await this.revealFile(change.filePath, preserveFocus);
      return;
    }
    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(change.filePath));
    const editor = await vscode.window.showTextDocument(doc, {
      preview: false,
      preserveFocus,
    });
    const range = rangeForHunk(doc, change.hunks[0]);
    editor.selection = new vscode.Selection(range.start, range.start);
    editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
  }

  private resolveLensTarget(document: vscode.TextDocument): { change: PendingChange } | null {
    if (document.uri.scheme === "file") {
      const change = this.getChangeForPath(document.uri.fsPath);
      return change ? { change } : null;
    }
    if (document.uri.scheme !== VIRTUAL_DIFF_SCHEME) {
      return null;
    }
    const info = parseReviewUri(document.uri);
    if (!info || info.side !== "after") {
      return null;
    }
    const change = this.changes.get(info.changeId);
    return change ? { change } : null;
  }

  private sortedChanges(): PendingChange[] {
    return [...this.changes.values()].sort((a, b) => {
      if (a.updatedAt !== b.updatedAt) {
        return a.updatedAt - b.updatedAt;
      }
      return a.shortPath.localeCompare(b.shortPath);
    });
  }

  private syncVirtualDocuments(change: PendingChange): void {
    for (const [key] of this.virtualDocuments) {
      const uri = vscode.Uri.parse(key);
      const info = parseReviewUri(uri);
      if (!info || info.changeId !== change.id) {
        continue;
      }
      this.virtualDocuments.set(key, info.side === "before" ? change.beforeContent : change.afterContent);
      this.contentEmitter.fire(uri);
    }
  }

  private async revealReviewedHunk(change: PendingChange, hunk: PendingHunk): Promise<void> {
    const visibleEditor = vscode.window.visibleTextEditors.find((editor) => {
      if (editor.document.uri.scheme === VIRTUAL_DIFF_SCHEME) {
        const info = parseReviewUri(editor.document.uri);
        return info?.changeId === change.id && info.side === "after";
      }
      return normalizeFilePath(editor.document.uri.fsPath) === normalizeFilePath(change.filePath);
    });

    if (visibleEditor) {
      revealHunkInEditor(visibleEditor, hunk);
      return;
    }

    await this.reviewFile(change.id);
    setTimeout(() => {
      const editor = vscode.window.visibleTextEditors.find((candidate) => {
        const info = parseReviewUri(candidate.document.uri);
        return info?.changeId === change.id && info.side === "after";
      });
      if (editor) {
        revealHunkInEditor(editor, hunk);
      }
    }, REVIEW_REVEAL_DELAY_MS);
  }

  private getReviewHtml(_webview: vscode.Webview): string {
    const nonce = getNonce();
    return /*html*/ `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>CrabCode Review</title>
  <style nonce="${nonce}">
    :root {
      --bg: var(--vscode-editor-background);
      --fg: var(--vscode-editor-foreground);
      --muted: var(--vscode-descriptionForeground);
      --border: color-mix(in srgb, var(--vscode-widget-border, #454545) 70%, transparent);
      --surface: color-mix(in srgb, var(--vscode-editor-background) 78%, var(--vscode-sideBar-background));
      --surface-2: color-mix(in srgb, var(--vscode-editor-background) 88%, var(--vscode-input-background));
      --accent: var(--vscode-focusBorder, #3794ff);
      --green: var(--vscode-terminal-ansiGreen, #89d185);
      --red: var(--vscode-terminal-ansiRed, #f48771);
      --code-font: var(--vscode-editor-font-family, monospace);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      height: 100vh;
      background: var(--bg);
      color: var(--fg);
      font-family: var(--vscode-font-family);
      font-size: var(--vscode-font-size);
      overflow: hidden;
    }
    button {
      font: inherit;
    }
    .shell {
      display: grid;
      grid-template-columns: minmax(180px, 260px) minmax(0, 1fr);
      height: 100vh;
      min-width: 0;
    }
    .sidebar {
      min-width: 0;
      border-right: 1px solid var(--border);
      background: var(--surface);
      overflow-y: auto;
      padding: 8px;
    }
    .side-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 4px 5px 8px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 650;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .file-item {
      width: 100%;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 6px;
      align-items: center;
      border: 1px solid transparent;
      border-radius: 7px;
      background: transparent;
      color: var(--fg);
      padding: 7px 8px;
      cursor: pointer;
      text-align: left;
      min-width: 0;
    }
    .file-item:hover {
      background: color-mix(in srgb, var(--accent) 10%, transparent);
    }
    .file-item.active {
      border-color: color-mix(in srgb, var(--accent) 38%, var(--border));
      background: color-mix(in srgb, var(--accent) 14%, transparent);
    }
    .file-name {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 12px;
      font-weight: 600;
    }
    .stats {
      font-family: var(--code-font);
      font-size: 11px;
      white-space: nowrap;
    }
    .add { color: var(--green); }
    .del { color: var(--red); }
    .main {
      min-width: 0;
      overflow: auto;
      display: flex;
      flex-direction: column;
    }
    .toolbar {
      position: sticky;
      top: 0;
      z-index: 5;
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
      padding: 9px 12px;
      border-bottom: 1px solid var(--border);
      background: color-mix(in srgb, var(--bg) 92%, transparent);
      backdrop-filter: blur(12px);
    }
    .title {
      min-width: 0;
      flex: 1;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .path {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-weight: 650;
    }
    .count {
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    .btn {
      border: 1px solid var(--border);
      background: var(--surface-2);
      color: var(--fg);
      border-radius: 6px;
      padding: 4px 9px;
      cursor: pointer;
      line-height: 1.25;
      white-space: nowrap;
    }
    .btn:hover {
      border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
      background: color-mix(in srgb, var(--accent) 12%, var(--surface-2));
    }
    .btn.primary {
      border-color: color-mix(in srgb, var(--vscode-button-background, var(--accent)) 82%, var(--border));
      background: var(--vscode-button-background, var(--accent));
      color: var(--vscode-button-foreground, white);
    }
    .btn.icon {
      padding: 4px 7px;
      min-width: 28px;
    }
    .content {
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .empty {
      margin: auto;
      color: var(--muted);
      border: 1px dashed var(--border);
      border-radius: 8px;
      padding: 18px;
    }
    .hunk {
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      background: var(--surface);
    }
    .hunk-head {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 7px 8px;
      border-bottom: 1px solid var(--border);
      background: color-mix(in srgb, var(--surface) 82%, var(--bg));
    }
    .hunk-title {
      flex: 1;
      min-width: 0;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }
    .diff-grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      min-width: 0;
    }
    .pane {
      min-width: 0;
      overflow-x: auto;
    }
    .pane.old {
      border-right: 1px solid var(--border);
    }
    .pane-title {
      position: sticky;
      top: 0;
      padding: 5px 8px;
      color: var(--muted);
      border-bottom: 1px solid var(--border);
      background: var(--surface);
      font-size: 11px;
      font-weight: 650;
      z-index: 1;
    }
    .line {
      display: grid;
      grid-template-columns: 44px minmax(0, 1fr);
      min-height: 20px;
      font-family: var(--code-font);
      font-size: var(--vscode-editor-font-size, 13px);
      line-height: 1.45;
    }
    .line.noop {
      opacity: 0.45;
    }
    .line.oldline {
      background: color-mix(in srgb, var(--red) 18%, transparent);
    }
    .line.newline {
      background: color-mix(in srgb, var(--green) 18%, transparent);
    }
    .ln {
      color: var(--muted);
      text-align: right;
      padding: 0 8px 0 4px;
      user-select: none;
    }
    .code {
      white-space: pre;
      padding-right: 12px;
    }
    @media (max-width: 860px) {
      .shell { grid-template-columns: 1fr; }
      .sidebar { display: none; }
      .diff-grid { grid-template-columns: 1fr; }
      .pane.old { border-right: none; border-bottom: 1px solid var(--border); }
      .toolbar, .hunk-head { flex-wrap: wrap; }
    }
  </style>
</head>
<body>
  <div id="root" class="shell"></div>
  <script nonce="${nonce}">
    (function() {
      const vscode = acquireVsCodeApi();
      const root = document.getElementById('root');
      let state = null;

      function send(msg) {
        vscode.postMessage(msg);
      }

      function render() {
        if (!state || !state.file) {
          root.className = '';
          root.innerHTML = '<div class="empty">No pending edits.</div>';
          return;
        }
        root.className = 'shell';
        const file = state.file;
        root.innerHTML = '';
        root.appendChild(renderSidebar(state));
        const main = document.createElement('main');
        main.className = 'main';
        main.appendChild(renderToolbar(state));
        const content = document.createElement('div');
        content.className = 'content';
        file.hunks.forEach(function(hunk) {
          content.appendChild(renderHunk(file, hunk));
        });
        main.appendChild(content);
        root.appendChild(main);
      }

      function renderSidebar(s) {
        const aside = document.createElement('aside');
        aside.className = 'sidebar';
        const title = document.createElement('div');
        title.className = 'side-title';
        title.textContent = s.totalFiles + (s.totalFiles === 1 ? ' File' : ' Files');
        aside.appendChild(title);
        s.files.forEach(function(file) {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'file-item' + (s.file && s.file.id === file.id ? ' active' : '');
          btn.title = file.path;
          btn.addEventListener('click', function() {
            send({ type: 'selectFile', changeId: file.id });
          });
          const name = document.createElement('span');
          name.className = 'file-name';
          name.textContent = file.shortPath;
          const stats = document.createElement('span');
          stats.className = 'stats';
          stats.innerHTML = '<span class="add">+' + file.added + '</span> <span class="del">-' + file.removed + '</span>';
          btn.appendChild(name);
          btn.appendChild(stats);
          aside.appendChild(btn);
        });
        return aside;
      }

      function renderToolbar(s) {
        const file = s.file;
        const bar = document.createElement('div');
        bar.className = 'toolbar';
        const title = document.createElement('div');
        title.className = 'title';
        const path = document.createElement('span');
        path.className = 'path';
        path.title = file.path;
        path.textContent = file.shortPath;
        const count = document.createElement('span');
        count.className = 'count';
        count.textContent = (s.currentIndex + 1) + ' of ' + s.totalFiles;
        title.appendChild(path);
        title.appendChild(count);
        bar.appendChild(title);
        bar.appendChild(button('‹', 'Previous file', 'icon', function() {
          send({ type: 'moveFile', changeId: file.id, offset: -1 });
        }));
        bar.appendChild(button('›', 'Next file', 'icon', function() {
          send({ type: 'moveFile', changeId: file.id, offset: 1 });
        }));
        bar.appendChild(button('Undo File', 'Undo file', '', function() {
          send({ type: 'reviewAction', action: 'undoFile', changeId: file.id });
        }));
        bar.appendChild(button('Keep File', 'Keep file', 'primary', function() {
          send({ type: 'reviewAction', action: 'keepFile', changeId: file.id });
        }));
        bar.appendChild(button('Native Diff', 'Open VS Code diff', '', function() {
          send({ type: 'openNativeDiff', changeId: file.id });
        }));
        bar.appendChild(button('Open File', 'Open changed file', '', function() {
          send({ type: 'openFile', changeId: file.id });
        }));
        return bar;
      }

      function renderHunk(file, hunk) {
        const wrap = document.createElement('section');
        wrap.className = 'hunk';
        wrap.id = 'hunk-' + hunk.id;
        const head = document.createElement('div');
        head.className = 'hunk-head';
        const title = document.createElement('div');
        title.className = 'hunk-title';
        title.textContent = 'Block ' + (hunk.index + 1) + ' of ' + hunk.total +
          ' · +' + hunk.newLineCount + ' -' + hunk.oldLineCount;
        head.appendChild(title);
        head.appendChild(button('‹ Block', 'Previous block', '', function() {
          scrollBlock(hunk.index - 1);
        }));
        head.appendChild(button('Block ›', 'Next block', '', function() {
          scrollBlock(hunk.index + 1);
        }));
        head.appendChild(button('Undo Block', 'Undo block', '', function() {
          send({ type: 'reviewAction', action: 'undoHunk', changeId: file.id, hunkId: hunk.id });
        }));
        head.appendChild(button('Keep Block', 'Keep block', 'primary', function() {
          send({ type: 'reviewAction', action: 'keepHunk', changeId: file.id, hunkId: hunk.id });
        }));
        wrap.appendChild(head);

        const grid = document.createElement('div');
        grid.className = 'diff-grid';
        grid.appendChild(renderPane('old', 'Before', hunk.oldStartLine, hunk.oldLines, hunk.oldLineCount));
        grid.appendChild(renderPane('new', 'After', hunk.newStartLine, hunk.newLines, hunk.newLineCount));
        wrap.appendChild(grid);
        return wrap;
      }

      function renderPane(kind, title, startLine, lines, lineCount) {
        const pane = document.createElement('div');
        pane.className = 'pane ' + kind;
        const label = document.createElement('div');
        label.className = 'pane-title';
        label.textContent = title;
        pane.appendChild(label);
        if (!lines || lines.length === 0) {
          pane.appendChild(renderLine('', '', 'noop'));
          return pane;
        }
        lines.forEach(function(line, idx) {
          pane.appendChild(renderLine(String(startLine + idx), line, kind === 'old' ? 'oldline' : 'newline'));
        });
        return pane;
      }

      function renderLine(no, code, cls) {
        const row = document.createElement('div');
        row.className = 'line ' + cls;
        const ln = document.createElement('span');
        ln.className = 'ln';
        ln.textContent = no;
        const body = document.createElement('span');
        body.className = 'code';
        body.textContent = code || ' ';
        row.appendChild(ln);
        row.appendChild(body);
        return row;
      }

      function button(text, title, extra, onClick) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn' + (extra ? ' ' + extra : '');
        btn.title = title;
        btn.textContent = text;
        btn.addEventListener('click', onClick);
        return btn;
      }

      function scrollBlock(index) {
        if (!state || !state.file || state.file.hunks.length === 0) return;
        const total = state.file.hunks.length;
        const next = (index + total) % total;
        const hunk = state.file.hunks[next];
        const el = document.getElementById('hunk-' + hunk.id);
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }

      window.addEventListener('message', function(event) {
        const msg = event.data;
        if (!msg || msg.type !== 'reviewState') return;
        state = msg.state;
        render();
      });

      send({ type: 'ready' });
    })();
  </script>
</body>
</html>`;
  }

  private async revealFile(filePath: string, preserveFocus = true): Promise<void> {
    try {
      const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(filePath));
      await vscode.window.showTextDocument(doc, {
        preview: true,
        preserveFocus,
      });
    } catch {
      // The file may have been deleted as part of an undo.
    }
  }
}

function isEditOrWriteTool(toolName: string): boolean {
  const normalized = toolName.toLowerCase();
  return normalized === "edit" || normalized === "write";
}

function firstString(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value !== "string") {
      continue;
    }
    const trimmed = value.trim();
    if (trimmed) {
      return trimmed;
    }
  }
  return null;
}

async function readTextFile(filePath: string): Promise<TextRead> {
  try {
    const bytes = await vscode.workspace.fs.readFile(vscode.Uri.file(filePath));
    return { exists: true, content: Buffer.from(bytes).toString("utf8") };
  } catch {
    return { exists: false, content: "" };
  }
}

async function writeTextFile(filePath: string, content: string): Promise<void> {
  await vscode.workspace.fs.createDirectory(vscode.Uri.file(path.dirname(filePath)));
  await vscode.workspace.fs.writeFile(vscode.Uri.file(filePath), Buffer.from(content, "utf8"));
}

async function deleteFileIfExists(filePath: string): Promise<void> {
  try {
    await vscode.workspace.fs.delete(vscode.Uri.file(filePath), {
      recursive: false,
      useTrash: false,
    });
  } catch {
    // Already gone.
  }
}

function computeLineDiff(before: string, after: string): DiffResult {
  const oldLines = splitLines(before);
  const newLines = splitLines(after);
  const ops = buildLineOps(oldLines, newLines);
  const stats = {
    added: ops.filter((op) => op.type === "insert").length,
    removed: ops.filter((op) => op.type === "delete").length,
  };
  const segments = changedSegments(ops);
  const merged = mergeSegments(segments);
  const hunks = merged.map((segment, index) => ({
    id: `hunk-${index + 1}`,
    oldStartLine: segment.oldStart + 1,
    oldLineCount: segment.oldEnd - segment.oldStart,
    newStartLine: segment.newStart + 1,
    newLineCount: segment.newEnd - segment.newStart,
    oldLines: oldLines.slice(segment.oldStart, segment.oldEnd),
    newLines: newLines.slice(segment.newStart, segment.newEnd),
  }));
  return { hunks, stats };
}

function buildLineOps(oldLines: string[], newLines: string[]): LineOp[] {
  const cells = (oldLines.length + 1) * (newLines.length + 1);
  if (cells > MAX_LCS_CELLS) {
    return fallbackLineOps(oldLines, newLines);
  }

  const width = newLines.length + 1;
  const table = new Uint32Array((oldLines.length + 1) * width);

  for (let i = oldLines.length - 1; i >= 0; i--) {
    for (let j = newLines.length - 1; j >= 0; j--) {
      const idx = i * width + j;
      if (oldLines[i] === newLines[j]) {
        table[idx] = table[(i + 1) * width + j + 1] + 1;
      } else {
        table[idx] = Math.max(table[(i + 1) * width + j], table[i * width + j + 1]);
      }
    }
  }

  const ops: LineOp[] = [];
  let i = 0;
  let j = 0;
  while (i < oldLines.length && j < newLines.length) {
    if (oldLines[i] === newLines[j]) {
      ops.push({ type: "equal", line: oldLines[i] });
      i++;
      j++;
    } else if (table[(i + 1) * width + j] >= table[i * width + j + 1]) {
      ops.push({ type: "delete", line: oldLines[i] });
      i++;
    } else {
      ops.push({ type: "insert", line: newLines[j] });
      j++;
    }
  }
  while (i < oldLines.length) {
    ops.push({ type: "delete", line: oldLines[i++] });
  }
  while (j < newLines.length) {
    ops.push({ type: "insert", line: newLines[j++] });
  }
  return ops;
}

function fallbackLineOps(oldLines: string[], newLines: string[]): LineOp[] {
  let prefix = 0;
  while (
    prefix < oldLines.length &&
    prefix < newLines.length &&
    oldLines[prefix] === newLines[prefix]
  ) {
    prefix++;
  }

  let oldEnd = oldLines.length;
  let newEnd = newLines.length;
  while (
    oldEnd > prefix &&
    newEnd > prefix &&
    oldLines[oldEnd - 1] === newLines[newEnd - 1]
  ) {
    oldEnd--;
    newEnd--;
  }

  return [
    ...oldLines.slice(0, prefix).map((line) => ({ type: "equal" as const, line })),
    ...oldLines.slice(prefix, oldEnd).map((line) => ({ type: "delete" as const, line })),
    ...newLines.slice(prefix, newEnd).map((line) => ({ type: "insert" as const, line })),
    ...oldLines.slice(oldEnd).map((line) => ({ type: "equal" as const, line })),
  ];
}

function changedSegments(ops: LineOp[]): Array<{
  oldStart: number;
  oldEnd: number;
  newStart: number;
  newEnd: number;
}> {
  const segments: Array<{
    oldStart: number;
    oldEnd: number;
    newStart: number;
    newEnd: number;
  }> = [];
  let oldIndex = 0;
  let newIndex = 0;
  let current: (typeof segments)[number] | null = null;

  for (const op of ops) {
    if (op.type === "equal") {
      if (current) {
        current.oldEnd = oldIndex;
        current.newEnd = newIndex;
        segments.push(current);
        current = null;
      }
      oldIndex++;
      newIndex++;
      continue;
    }

    if (!current) {
      current = {
        oldStart: oldIndex,
        oldEnd: oldIndex,
        newStart: newIndex,
        newEnd: newIndex,
      };
    }
    if (op.type === "delete") {
      oldIndex++;
    } else {
      newIndex++;
    }
    current.oldEnd = oldIndex;
    current.newEnd = newIndex;
  }

  if (current) {
    segments.push(current);
  }
  return segments;
}

function mergeSegments(
  segments: Array<{ oldStart: number; oldEnd: number; newStart: number; newEnd: number }>,
): Array<{ oldStart: number; oldEnd: number; newStart: number; newEnd: number }> {
  const merged: Array<{ oldStart: number; oldEnd: number; newStart: number; newEnd: number }> = [];
  for (const segment of segments) {
    const previous = merged[merged.length - 1];
    if (
      previous &&
      segment.oldStart - previous.oldEnd <= CONTEXT_LINES * 2 &&
      segment.newStart - previous.newEnd <= CONTEXT_LINES * 2
    ) {
      previous.oldEnd = segment.oldEnd;
      previous.newEnd = segment.newEnd;
    } else {
      merged.push({ ...segment });
    }
  }
  return merged;
}

function splitLines(text: string): string[] {
  if (!text) {
    return [];
  }
  const normalized = text.replace(/\r\n/g, "\n");
  const lines = normalized.split("\n");
  if (lines.length > 0 && lines[lines.length - 1] === "") {
    lines.pop();
  }
  return lines;
}

function joinLines(lines: string[], eol: string, finalNewline: boolean): string {
  if (lines.length === 0) {
    return finalNewline ? eol : "";
  }
  return lines.join(eol) + (finalNewline ? eol : "");
}

function hasFinalNewline(text: string): boolean {
  return text.endsWith("\n") || text.endsWith("\r\n");
}

function detectEol(text: string): string {
  return text.includes("\r\n") ? "\r\n" : "\n";
}

function findHunkStart(lines: string[], hunk: PendingHunk): number {
  const preferred = Math.min(Math.max(hunk.newStartLine - 1, 0), lines.length);
  if (hunk.newLineCount === 0) {
    return preferred;
  }
  if (sequenceMatches(lines, preferred, hunk.newLines)) {
    return preferred;
  }
  return findSequence(lines, hunk.newLines);
}

function sequenceMatches(lines: string[], start: number, expected: string[]): boolean {
  if (start < 0 || start + expected.length > lines.length) {
    return false;
  }
  for (let i = 0; i < expected.length; i++) {
    if (lines[start + i] !== expected[i]) {
      return false;
    }
  }
  return true;
}

function findSequence(lines: string[], expected: string[]): number {
  if (expected.length === 0) {
    return 0;
  }
  for (let i = 0; i <= lines.length - expected.length; i++) {
    if (sequenceMatches(lines, i, expected)) {
      return i;
    }
  }
  return -1;
}

function applyHunkToBase(baseContent: string, hunk: PendingHunk, afterContent: string): string {
  const baseLines = splitLines(baseContent);
  const start = Math.min(Math.max(hunk.oldStartLine - 1, 0), baseLines.length);
  if (!sequenceMatches(baseLines, start, hunk.oldLines)) {
    const found = findSequence(baseLines, hunk.oldLines);
    if (found >= 0) {
      baseLines.splice(found, hunk.oldLineCount, ...hunk.newLines);
    } else {
      baseLines.splice(start, hunk.oldLineCount, ...hunk.newLines);
    }
  } else {
    baseLines.splice(start, hunk.oldLineCount, ...hunk.newLines);
  }
  return joinLines(baseLines, detectEol(afterContent || baseContent), hasFinalNewline(afterContent));
}

function parseReviewUri(uri: vscode.Uri): { changeId: string; side: "before" | "after" } | null {
  const params = new URLSearchParams(uri.query);
  const queryChangeId = params.get("changeId");
  const querySide = params.get("side");
  if (queryChangeId && (querySide === "before" || querySide === "after")) {
    return { changeId: queryChangeId, side: querySide };
  }

  const parts = uri.path.split("/").filter(Boolean);
  const [changeId, side] = parts;
  if (changeId && (side === "before" || side === "after")) {
    return { changeId, side };
  }
  return null;
}

function revealHunkInEditor(editor: vscode.TextEditor, hunk: PendingHunk): void {
  const range = rangeForHunk(editor.document, hunk);
  editor.selection = new vscode.Selection(range.start, range.start);
  editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
}

function rangeForHunk(document: vscode.TextDocument, hunk: PendingHunk): vscode.Range {
  const maxLine = Math.max(0, document.lineCount - 1);
  const startLine = Math.min(Math.max(hunk.newStartLine - 1, 0), maxLine);
  const requestedEnd = hunk.newStartLine - 1 + Math.max(1, hunk.newLineCount);
  const endLine = Math.min(Math.max(startLine, requestedEnd - 1), maxLine);
  return new vscode.Range(
    new vscode.Position(startLine, 0),
    document.lineAt(endLine).range.end,
  );
}

function shortFilePath(filePath: string): string {
  const workspace = vscode.workspace.workspaceFolders?.find((folder) => {
    const root = normalizeFilePath(folder.uri.fsPath);
    const normalized = normalizeFilePath(filePath);
    return normalized === root || normalized.startsWith(root + path.sep);
  });
  if (!workspace) {
    return path.basename(filePath);
  }
  const relative = path.relative(workspace.uri.fsPath, filePath);
  return relative || path.basename(filePath);
}

function normalizeFilePath(filePath: string): string {
  return path.normalize(filePath);
}

function formatStats(stats: DiffStats): string {
  const parts = [];
  if (stats.added) {
    parts.push(`+${stats.added}`);
  }
  if (stats.removed) {
    parts.push(`-${stats.removed}`);
  }
  return parts.join(" ") || "no line changes";
}

function getNonce(): string {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let text = "";
  for (let i = 0; i < 32; i++) {
    text += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return text;
}
