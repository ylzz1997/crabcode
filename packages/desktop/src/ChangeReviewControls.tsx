import { useState } from "react";
import { Check, RefreshCw } from "lucide-react";
import type { ChangeReviewSelection, ChangeReviewTurn } from "./ChangeReview";
import type { GitReviewInfo, GitReviewScope } from "./types";
import { ReviewMenu } from "./ReviewMenu";
import { ALL_EDITS, editHistoryIndex } from "./reviewHistory";

export const GIT_SCOPE_LABELS: Record<GitReviewScope, string> = {
  uncommitted: "未提交", unstaged: "未暂存", staged: "已暂存", commit: "已提交", branch: "分支",
};

export function ChangeReviewControls({ turns, selection, gitInfo, gitLoading, unavailable, onSelect, onRefresh }: {
  turns: ChangeReviewTurn[];
  selection: ChangeReviewSelection;
  gitInfo: GitReviewInfo | null;
  gitLoading: boolean;
  unavailable: string;
  onSelect: (selection: ChangeReviewSelection) => void;
  onRefresh: () => void;
}) {
  const [number, setNumber] = useState("");
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const isGit = selection.source === "git";
  const historyId = !isGit ? selection.summaryId : null;
  const historyIndex = turns.findIndex((turn) => turn.id === historyId);
  const historyLabel = historyId === ALL_EDITS ? "全部编辑" : historyIndex === turns.length - 1 ? "最近一次编辑" : `第 ${historyIndex + 1} 次编辑`;
  const recent = turns[turns.length - 1];
  const selectHistory = (id: string) => onSelect({ summaryId: id, path: null, showAll: true });
  const available = Boolean(gitInfo?.available && !gitLoading);
  const ref = isGit ? selection.ref ?? (selection.scope === "branch" ? gitInfo?.default_base : gitInfo?.head) : null;
  const references = isGit && selection.scope === "commit"
    ? gitInfo?.commits.map((commit) => ({ id: commit.id, label: `${commit.id.slice(0, 7)} ${commit.subject}` })) ?? []
    : gitInfo?.branches.map((branch) => ({ id: branch, label: branch })) ?? [];

  return <>
    <ReviewMenu label={isGit ? GIT_SCOPE_LABELS[selection.scope] : "会话编辑"} ariaLabel="变更范围">
      {(close) => <>
        <button type="button" data-menu-option role="menuitemradio" aria-checked={!isGit} disabled={!recent}
          onClick={() => { selectHistory(historyId ?? recent.id); close(); }}><span>会话编辑</span>{!isGit && <Check />}</button>
        <hr />
        {!available && <div className="review-menu-hint">{gitLoading ? "正在检测 Git…" : unavailable}</div>}
        {(Object.keys(GIT_SCOPE_LABELS) as GitReviewScope[]).map((scope) => {
          const disabled = !available || ((scope === "commit" || scope === "branch") && !gitInfo?.head)
            || (scope === "branch" && !gitInfo?.branches.length);
          const disabledReason = !available ? unavailable : !gitInfo?.head ? "当前仓库还没有提交" : "没有可比较的分支";
          return <button type="button" data-menu-option role="menuitemradio" aria-checked={isGit && selection.scope === scope}
            disabled={disabled} title={disabled ? disabledReason : undefined} key={scope}
            onClick={() => { onSelect({ source: "git", scope, path: null, showAll: true }); setQuery(""); close(); }}>
            <span>{GIT_SCOPE_LABELS[scope]}</span>{isGit && selection.scope === scope && <Check />}
          </button>;
        })}
      </>}
    </ReviewMenu>
    <ReviewMenu label={isGit ? "编辑记录" : historyLabel} ariaLabel="编辑记录" width={280} role="dialog" disabled={!turns.length}>
      {(close) => <>
        <div className="change-review-turn-menu-heading">会话编辑记录 · 共 {turns.length} 次</div>
        {[
          { label: "全部", id: ALL_EDITS },
          { label: "最近一次", id: recent?.id },
          ...["前一次", "前二次", "前三次"].map((label, index) => ({ label, id: turns[turns.length - 2 - index]?.id })),
        ].map(({ label, id }) => <button type="button" key={label} data-menu-option disabled={!id}
          aria-pressed={historyId === id} onClick={() => { if (id) selectHistory(id); setError(""); close(); }}>
          <span>{label}</span>{historyId === id && <Check />}
        </button>)}
        <form className="review-history-form" onSubmit={(event) => {
          event.preventDefault();
          const index = editHistoryIndex(number, turns.length);
          if (index === null) { setError(`序号超出范围或格式不正确，共 ${turns.length} 次编辑`); return; }
          selectHistory(turns[index].id); setError(""); close();
        }}>
          <label htmlFor="review-history-number">跳转到第几次编辑</label>
          <div><input id="review-history-number" aria-label="编辑序号" inputMode="text" value={number}
            placeholder="如 5 或 -2" onChange={(event) => { setNumber(event.target.value); setError(""); }} />
            <button type="submit">查看</button></div>
          <small>正数从第 1 次开始；-1 是最近一次之前的编辑，-2 再往前一次。</small>
          {error && <p role="alert">{error}</p>}
        </form>
      </>}
    </ReviewMenu>
    {isGit && (selection.scope === "commit" || selection.scope === "branch") && <ReviewMenu
      label={references.find((item) => item.id === ref)?.label ?? "选择比较对象"}
      ariaLabel={selection.scope === "commit" ? "选择提交" : "选择比较分支"} width={340}>
      {(close) => <>
        <input className="review-reference-filter" aria-label="筛选比较对象" placeholder="筛选…" value={query} onChange={(event) => setQuery(event.target.value)} />
        {selection.scope === "commit" && <div className="review-menu-hint">最近 {references.length} 次提交</div>}
        {references.filter((item) => item.label.toLowerCase().includes(query.toLowerCase())).map((item) => (
          <button type="button" key={item.id} data-menu-option role="menuitemradio" aria-checked={item.id === ref}
            title={item.label} onClick={() => { onSelect({ ...selection, ref: item.id, path: null, showAll: true }); close(); }}>
            <span>{item.label}</span>{item.id === ref && <Check />}
          </button>
        ))}
      </>}
    </ReviewMenu>}
    <button type="button" className="icon-button tiny" title="刷新变更" aria-label="刷新变更" onClick={onRefresh}><RefreshCw /></button>
  </>;
}
