import { useMemo, useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight, FileCode, Folder, LoaderCircle, Search } from "lucide-react";
import { diffRows } from "./diffRows";
import { readFileEditSummary, type FileEditRecord, type FileEditSummaryData } from "./fileEditSummary";
import type { ChatItem, GitReviewScope } from "./types";

export interface ChangeReviewTurn {
  id: string;
  summary: FileEditSummaryData;
}

export type ChangeReviewSelection = { path: string | null; showAll: boolean } & (
  | { source?: "history"; summaryId: string; scope?: never; ref?: never }
  | { source: "git"; scope: GitReviewScope; ref?: string; summaryId?: never }
);

export interface WorkspaceChangeReview {
  turns: ChangeReviewTurn[];
  selection: ChangeReviewSelection | null;
  active: boolean;
  onOpen: (selection: ChangeReviewSelection) => void;
  onActivate: () => void;
  onClose: () => void;
}

export function changeReviewTurns(items: ChatItem[]): ChangeReviewTurn[] {
  return items.flatMap((item) => {
    if (item.kind !== "file_edit_summary") return [];
    const summary = readFileEditSummary(item.detail);
    return summary ? [{ id: item.id, summary }] : [];
  });
}

export function ChangeReviewStats({ added, removed }: { added: number; removed: number }) {
  if (added === 0 && removed === 0) return null;
  return (
    <span className="change-review-stats">
      {added > 0 && <em className="added">+{added}</em>}
      {removed > 0 && <em className="removed">-{removed}</em>}
    </span>
  );
}

interface TreeNode {
  name: string;
  path: string;
  file?: FileEditRecord;
  children: TreeNode[];
}

function buildTree(files: FileEditRecord[]): TreeNode[] {
  const roots: TreeNode[] = [];
  for (const file of files) {
    const parts = file.path.replace(/\\/g, "/").split("/").filter(Boolean);
    let level = roots;
    let accumulated = "";
    parts.forEach((part, index) => {
      accumulated = accumulated ? `${accumulated}/${part}` : part;
      const isFile = index === parts.length - 1;
      let node = level.find((item) => item.name === part && Boolean(item.file) === isFile);
      if (!node) {
        node = { name: part, path: accumulated, children: [], ...(isFile ? { file } : {}) };
        level.push(node);
      }
      if (isFile) node.file = file;
      level = node.children;
    });
  }
  const compact = (nodes: TreeNode[]): TreeNode[] => nodes.map((node) => {
    while (!node.file && node.children.length === 1 && !node.children[0].file) {
      const child = node.children[0];
      node = { ...child, name: `${node.name}/${child.name}` };
    }
    return { ...node, children: compact(node.children) };
  });
  return compact(roots);
}

function DiffBody({ file }: { file: FileEditRecord }) {
  const rows = diffRows(file.diff);
  if (!rows.length) {
    return <p className="change-review-missing">{file.note ?? "这次改动没有保存完整 diff，只能看到行数。"}</p>;
  }
  return (
    <div className="change-review-code">
      {rows.map((row, index) => row.kind === "gap" ? (
        <div className="change-review-gap" key={`gap-${index}`}>{row.gap} 行未修改</div>
      ) : (
        <div className={`change-review-line ${row.kind}`} key={`${row.kind}-${index}`}>
          <span className="change-review-number">{row.newNumber ?? row.oldNumber ?? ""}</span>
          <span
            className={`diff-line ${row.kind === "add" ? "added" : row.kind === "remove" ? "removed" : "context"}`}
            data-marker={row.kind === "add" ? "+" : row.kind === "remove" ? "-" : ""}
          >
            {row.text || " "}
          </span>
        </div>
      ))}
    </div>
  );
}

function TreeFiles({
  nodes,
  selectedPath,
  collapsed,
  onToggle,
  onSelect,
}: {
  nodes: TreeNode[];
  selectedPath: string | null;
  collapsed: ReadonlySet<string>;
  onToggle: (path: string) => void;
  onSelect: (path: string) => void;
}) {
  return (
    <ul className="change-review-tree-list">
      {nodes.map((node) => {
        if (node.file) {
          return (
            <li key={node.path}>
              <button
                type="button"
                className={`change-review-tree-file ${selectedPath === node.file.path ? "selected" : ""}`}
                title={node.file.path}
                aria-current={selectedPath === node.file.path ? "true" : undefined}
                onClick={() => onSelect(node.file!.path)}
              >
                <FileCode />
                <span>{node.name}</span>
                <ChangeReviewStats added={node.file.added} removed={node.file.removed} />
              </button>
            </li>
          );
        }
        const open = !collapsed.has(node.path);
        return (
          <li key={node.path}>
            <button type="button" className="change-review-tree-dir" title={node.path} aria-expanded={open} onClick={() => onToggle(node.path)}>
              {open ? <ChevronDown /> : <ChevronRight />}
              <Folder />
              <span>{node.name}</span>
            </button>
            {open && (
              <TreeFiles
                nodes={node.children}
                selectedPath={selectedPath}
                collapsed={collapsed}
                onToggle={onToggle}
                onSelect={onSelect}
              />
            )}
          </li>
        );
      })}
    </ul>
  );
}

export function ChangeReview({ summary, path, showAll, toolbar, history, loading = false, error, description, onShowAll }: {
  summary: FileEditSummaryData | null;
  path: string | null;
  showAll: boolean;
  toolbar: ReactNode;
  history?: ChangeReviewTurn[];
  loading?: boolean;
  error?: string | null;
  description?: string;
  onShowAll: () => void;
}) {
  const files = summary?.files ?? [];
  const selected = files.find((file) => file.path === path) ?? (showAll ? null : files[0] ?? null);
  const visible = (entries: FileEditRecord[]) => showAll && !path ? entries : entries.filter((file) => file.path === selected?.path);
  const articles = (entries: FileEditRecord[]) => entries.map((file) => (
    <article key={file.path}>
      <header className="change-review-file-heading">
        <FileCode /><span title={file.path}>{file.path}</span>
        <ChangeReviewStats added={file.added} removed={file.removed} />
      </header>
      <DiffBody file={file} />
    </article>
  ));
  return (
    <section className="project-file-preview change-review-diff" aria-label="差异">
      <div className="change-review-toolbar">
        {toolbar}
        {summary && !loading && !error && <>
          <ChangeReviewStats added={summary.added} removed={summary.removed} />
          <button type="button" className={`change-review-all ${showAll && !path ? "active" : ""}`} aria-pressed={showAll && !path} onClick={onShowAll}>
            全部 {files.length} 个文件
          </button>
        </>}
      </div>
      {description && <div className="change-review-description">{description}</div>}
      <div className="change-review-scroll">
        {loading ? <p className="change-review-status" role="status"><LoaderCircle className="spin" />正在读取变更…</p>
          : error ? <p className="change-review-missing" role="alert">{error}</p>
          : history ? history.map((turn, index) => visible(turn.summary.files).length > 0 && (
            <section className="change-review-history-group" key={turn.id}>
              <h3>第 {index + 1} 次编辑</h3>{articles(visible(turn.summary.files))}
            </section>
          )) : articles(visible(files))}
        {!loading && !error && !files.length && <p className="change-review-missing">这个范围没有文件变更。</p>}
      </div>
    </section>
  );
}

export function ChangeReviewFiles({ files, selectedPath, onSelectFile, onShowAll }: {
  files: FileEditRecord[];
  selectedPath: string | null;
  onSelectFile: (path: string) => void;
  onShowAll: () => void;
}) {
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set());
  const visibleFiles = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return needle ? files.filter((file) => file.path.toLocaleLowerCase().includes(needle)) : files;
  }, [files, query]);
  const tree = useMemo(() => buildTree(visibleFiles), [visibleFiles]);
  return (
    <div className="change-review-tree" aria-label="变更文件">
      <label className="change-review-search">
        <Search />
        <input
          value={query}
          placeholder="筛选文件..."
          aria-label="筛选文件"
          onChange={(event) => setQuery(event.target.value)}
        />
      </label>
      <button type="button" className={`change-review-tree-all ${selectedPath === null ? "selected" : ""}`} onClick={onShowAll}>
        全部变更 <span>{files.length}</span>
      </button>
      {visibleFiles.length ? (
        <TreeFiles
          nodes={tree}
          selectedPath={selectedPath}
          collapsed={collapsed}
          onToggle={(directory) => setCollapsed((current) => {
            const next = new Set(current);
            if (next.has(directory)) next.delete(directory);
            else next.add(directory);
            return next;
          })}
          onSelect={onSelectFile}
        />
      ) : <p className="change-review-missing">没有匹配的文件</p>}
    </div>
  );
}
