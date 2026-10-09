import type { ChatItem } from "./types";

export interface FileEditRecord {
  path: string;
  action: "create" | "modify" | "delete";
  added: number;
  removed: number;
  diff: string | null;
  note?: string | null;
}

export interface FileEditSummaryData {
  files: FileEditRecord[];
  added: number;
  removed: number;
}

const FILE_EDIT_TOOLS = new Set(["edit", "fileedit", "write", "filewrite", "applypatch"]);

function toolKey(name: string | undefined): string {
  return (name ?? "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

function normalizePath(value: string): string {
  return value.replace(/\\/g, "/").replace(/\/+$/, "");
}

export function relativeEditPath(path: string, cwd: string): string {
  const file = path.replace(/\\/g, "/");
  const root = normalizePath(cwd);
  if (!root) return file;
  if (file === root) return file.split("/").pop() || file;
  if (file.startsWith(`${root}/`)) return file.slice(root.length + 1);
  return file;
}

function countDiff(diff: string): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const line of diff.split("\n")) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) added += 1;
    else if (line.startsWith("-")) removed += 1;
  }
  return { added, removed };
}

function actionFromDiff(diff: string, added: number, removed: number): FileEditRecord["action"] {
  if (/^@@ -0,0 /m.test(diff)) return "create";
  if (/^@@ -\d+(?:,\d+)? \+0,0/m.test(diff)) return "delete";
  if (removed === 0 && added > 0 && diff.includes("@@ -0,")) return "create";
  return "modify";
}

function splitUnifiedDiff(text: string): FileEditRecord[] {
  const lines = text.split("\n");
  const files: FileEditRecord[] = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index] ?? "";
    const next = lines[index + 1] ?? "";
    if (line.startsWith("--- ") && next.startsWith("+++ ")) {
      const fromPath = line.slice(4).trim();
      const toPath = next.slice(4).trim();
      const path = fromPath.startsWith("a/") && toPath.startsWith("b/")
        ? toPath.slice(2)
        : (toPath === "/dev/null" ? fromPath : toPath);
      const chunk = [line, next];
      index += 2;
      while (index < lines.length) {
        const ahead = lines[index] ?? "";
        const after = lines[index + 1] ?? "";
        if (ahead.startsWith("--- ") && after.startsWith("+++ ")) break;
        chunk.push(ahead);
        index += 1;
      }
      const diff = chunk.join("\n");
      const stats = countDiff(diff);
      const action = toPath === "/dev/null"
        ? "delete"
        : fromPath === "/dev/null"
          ? "create"
          : actionFromDiff(diff, stats.added, stats.removed);
      if (path) files.push({ path, action, ...stats, diff });
      continue;
    }
    index += 1;
  }
  return files;
}

function parseUpdated(line: string): { path: string; added: number; removed: number; hasStats: boolean } | null {
  if (!line.startsWith("Updated ")) return null;
  let rest = line.slice("Updated ".length).trimEnd();
  rest = rest.replace(/\s+\(\d+\s+replacements\)$/, "");
  let added = 0;
  let removed = 0;
  let hasStats = false;
  const stats = rest.match(/\s+\[([^\]]+)\]$/);
  if (stats) {
    hasStats = true;
    added = Number(stats[1].match(/\+(\d+)/)?.[1] ?? 0);
    removed = Number(stats[1].match(/-(\d+)/)?.[1] ?? 0);
    rest = rest.slice(0, -stats[0].length);
  }
  rest = rest.replace(/\s+\((?:lines?)\s+\d+(?:-\d+)?\)$/, "");
  const path = rest.trim();
  if (!path) return null;
  return { path, added, removed, hasStats };
}

function parseCreated(line: string): { path: string; added: number } | null {
  const match = line.match(/^Created\s+(.+)\s+\((\d+)\s+lines\)\.?$/);
  if (!match) return null;
  return { path: match[1], added: Number(match[2]) };
}

function parsePatchFiles(patch: string): FileEditRecord[] {
  const records: FileEditRecord[] = [];
  let current: FileEditRecord | null = null;
  const flush = () => {
    if (current) records.push(current);
  };
  for (const line of patch.split("\n")) {
    const action = line.match(/^\*\*\* (Add|Update|Delete) File: (.+)$/);
    if (action) {
      flush();
      current = {
        path: action[2].trim(),
        action: action[1] === "Add" ? "create" : action[1] === "Delete" ? "delete" : "modify",
        added: 0,
        removed: 0,
        diff: null,
      };
      continue;
    }
    const move = line.match(/^\*\*\* Move to: (.+)$/);
    if (move && current) {
      current.path = move[1].trim();
      continue;
    }
    if (!current || line.startsWith("***")) continue;
    if (line.startsWith("+") && !line.startsWith("+++")) current.added += 1;
    else if (line.startsWith("-") && !line.startsWith("---")) current.removed += 1;
  }
  flush();
  return records.filter((record) => record.path);
}

function inputRecord(item: ChatItem): Record<string, unknown> {
  if (item.input && typeof item.input === "object") return item.input;
  if (item.detail && typeof item.detail === "object" && !Array.isArray(item.detail)) {
    return item.detail as Record<string, unknown>;
  }
  return {};
}

function resultText(item: ChatItem): string {
  if (typeof item.result === "string") return item.result;
  if (typeof item.detail === "string") return item.detail;
  return "";
}

function matchingDiff(path: string, diffs: FileEditRecord[], cwd: string): FileEditRecord | undefined {
  const target = relativeEditPath(path, cwd);
  return diffs.find((diff) => relativeEditPath(diff.path, cwd) === target) ?? (diffs.length === 1 ? diffs[0] : undefined);
}

function editsFromTool(item: ChatItem, cwd: string): FileEditRecord[] {
  if (item.kind !== "tool" || item.isError || item.status === "running" || item.status === "pending") return [];
  const key = toolKey(item.title);
  if (!FILE_EDIT_TOOLS.has(key)) return [];
  const result = resultText(item);
  const firstLine = result.split("\n")[0] ?? "";
  const input = inputRecord(item);

  if (key === "applypatch" || firstLine.startsWith("Applied patch to ")) {
    const diffs = splitUnifiedDiff(result);
    if (diffs.length) return diffs;
    if (typeof input.patch === "string" && input.patch) return parsePatchFiles(input.patch);
    const affected = Array.isArray(input.affected_paths)
      ? input.affected_paths.filter((path): path is string => typeof path === "string" && path.length > 0)
      : [];
    return affected.map((path) => ({ path, action: "modify" as const, added: 0, removed: 0, diff: null }));
  }

  const created = parseCreated(firstLine);
  if (created) {
    return [{ path: created.path, action: "create", added: created.added, removed: 0, diff: null }];
  }

  const updated = parseUpdated(firstLine);
  if (!updated) return [];
  const diff = matchingDiff(updated.path, splitUnifiedDiff(result), cwd);
  return [{
    path: updated.path,
    action: "modify",
    added: updated.hasStats ? updated.added : diff?.added ?? 0,
    removed: updated.hasStats ? updated.removed : diff?.removed ?? 0,
    diff: diff?.diff ?? null,
  }];
}

function editsFromFileChange(item: ChatItem): FileEditRecord[] {
  if (item.kind !== "file_change") return [];
  const path = item.path || item.title || "";
  if (!path) return [];
  const diff = item.diff?.trim() ? item.diff : null;
  const stats = diff ? countDiff(diff) : { added: 0, removed: 0 };
  const action = item.action === "create" || item.action === "delete" ? item.action : "modify";
  return [{ path, action, ...stats, diff }];
}

function mergeRecords(existing: FileEditRecord, next: FileEditRecord): FileEditRecord {
  const sameStats = existing.added === next.added && existing.removed === next.removed;
  const existingDiff = existing.diff ?? "";
  const nextDiff = next.diff ?? "";
  if (!nextDiff && next.added === 0 && next.removed === 0) return existing;
  if (!existingDiff && existing.added === 0 && existing.removed === 0 && (nextDiff || next.added || next.removed)) {
    return { ...next, path: existing.path };
  }
  if (sameStats) {
    const action = next.action === "delete" ? "delete" : existing.action === "create" ? "create" : next.action;
    return {
      ...existing,
      action,
      diff: nextDiff.length > existingDiff.length ? nextDiff : existingDiff || null,
    };
  }
  const action = next.action === "delete" || existing.action !== "create" ? next.action : "create";
  return {
    path: existing.path,
    action,
    added: existing.added + next.added,
    removed: existing.removed + next.removed,
    diff: [existingDiff, nextDiff].filter(Boolean).join("\n") || null,
  };
}

export function summarizeFileEdits(items: ChatItem[], cwd: string): FileEditSummaryData | null {
  const order: string[] = [];
  const byPath = new Map<string, FileEditRecord>();
  for (const item of items) {
    if (item.kind === "file_edit_summary" || item.kind === "turn_duration") continue;
    const records = item.kind === "file_change" ? editsFromFileChange(item) : editsFromTool(item, cwd);
    for (const record of records) {
      const path = relativeEditPath(record.path, cwd);
      if (!path) continue;
      const next = { ...record, path };
      const existing = byPath.get(path);
      if (!existing) {
        order.push(path);
        byPath.set(path, next);
        continue;
      }
      byPath.set(path, mergeRecords(existing, next));
    }
  }
  if (!order.length) return null;
  const files = order.map((path) => byPath.get(path)!);
  return {
    files,
    added: files.reduce((sum, file) => sum + file.added, 0),
    removed: files.reduce((sum, file) => sum + file.removed, 0),
  };
}

export function fileEditSummaryItem(id: string, summary: FileEditSummaryData): ChatItem {
  return {
    id,
    kind: "file_edit_summary",
    title: `已编辑 ${summary.files.length} 个文件`,
    detail: summary,
    status: "complete",
  };
}

export function readFileEditSummary(detail: unknown): FileEditSummaryData | null {
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) return null;
  const record = detail as Partial<FileEditSummaryData>;
  if (!Array.isArray(record.files)) return null;
  const files = record.files.flatMap((file) => {
    if (!file || typeof file !== "object") return [];
    const entry = file as Partial<FileEditRecord>;
    if (typeof entry.path !== "string" || !entry.path) return [];
    const action = entry.action === "create" || entry.action === "delete" ? entry.action : "modify";
    return [{
      path: entry.path,
      action,
      added: typeof entry.added === "number" ? entry.added : 0,
      removed: typeof entry.removed === "number" ? entry.removed : 0,
      diff: typeof entry.diff === "string" && entry.diff ? entry.diff : null,
    } satisfies FileEditRecord];
  });
  if (!files.length) return null;
  return {
    files,
    added: typeof record.added === "number" ? record.added : files.reduce((sum, file) => sum + file.added, 0),
    removed: typeof record.removed === "number" ? record.removed : files.reduce((sum, file) => sum + file.removed, 0),
  };
}

export function latestFileEditSummaryId(items: ChatItem[]): string | undefined {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    if (items[index]?.kind === "file_edit_summary") return items[index].id;
  }
  return undefined;
}
