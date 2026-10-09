import { useState } from "react";
import { ChevronDown, FileDiff, RotateCcw } from "lucide-react";
import { readFileEditSummary } from "./fileEditSummary";
import type { ChatItem } from "./types";

const PREVIEW_COUNT = 3;

export interface FileEditReviewRequest {
  path: string | null;
  all: boolean;
}

function StatPair({ added, removed }: { added: number; removed: number }) {
  if (added === 0 && removed === 0) return null;
  return (
    <span className="file-edit-summary-stats">
      {added > 0 && <em className="added">+{added}</em>}
      {removed > 0 && <em className="removed">-{removed}</em>}
    </span>
  );
}

export function FileEditSummaryCard({ item, onUndo, onOpenReview }: {
  item: ChatItem;
  onUndo?: () => Promise<void> | void;
  onOpenReview?: (request: FileEditReviewRequest) => void;
}) {
  const summary = readFileEditSummary(item.detail);
  const [namesExpanded, setNamesExpanded] = useState(false);
  const [undoBusy, setUndoBusy] = useState(false);
  const [undoError, setUndoError] = useState<string | null>(null);
  if (!summary) return null;

  const listed = namesExpanded ? summary.files : summary.files.slice(0, PREVIEW_COUNT);
  const hiddenCount = summary.files.length - listed.length;
  const undo = () => {
    if (!onUndo || undoBusy) return;
    setUndoError(null);
    setUndoBusy(true);
    void Promise.resolve(onUndo()).then(() => undefined, (error: unknown) => {
      setUndoError(error instanceof Error ? error.message : String(error));
    }).finally(() => setUndoBusy(false));
  };

  return (
    <article className="file-edit-summary" aria-label={item.title ?? `已编辑 ${summary.files.length} 个文件`}>
      <header className="file-edit-summary-header">
        <span className="file-edit-summary-icon" aria-hidden="true"><FileDiff /></span>
        <span className="file-edit-summary-title">
          <strong>已编辑 {summary.files.length} 个文件</strong>
          <StatPair added={summary.added} removed={summary.removed} />
        </span>
        <span className="file-edit-summary-actions">
          {onUndo && (
            <button type="button" className="file-edit-summary-undo" disabled={undoBusy} onClick={undo}>
              {undoBusy ? "正在撤销" : "撤销"}
              <RotateCcw />
            </button>
          )}
          <button type="button" className="file-edit-summary-review" onClick={() => onOpenReview?.({ path: null, all: true })}>
            查看变更
          </button>
        </span>
      </header>
      {undoError && <p className="file-edit-summary-error">{undoError}</p>}
      <div className="file-edit-summary-files">
        {listed.map((file) => (
          <button
            type="button"
            className="file-edit-summary-file"
            key={file.path}
            onClick={() => onOpenReview?.({ path: file.path, all: false })}
          >
            <span className="file-edit-summary-path" title={file.path}>{file.path}</span>
            <StatPair added={file.added} removed={file.removed} />
          </button>
        ))}
      </div>
      {hiddenCount > 0 && (
        <button type="button" className="file-edit-summary-more" onClick={() => setNamesExpanded(true)}>
          再显示 {hiddenCount} 个文件
          <ChevronDown />
        </button>
      )}
    </article>
  );
}
