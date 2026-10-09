import type { ChangeReviewTurn } from "./ChangeReview";
import type { FileEditSummaryData } from "./fileEditSummary";

export const ALL_EDITS = "__all_edits__";

export function editHistoryIndex(value: string, count: number): number | null {
  if (!/^-?\d+$/.test(value.trim())) return null;
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number === 0) return null;
  const index = number > 0 ? number - 1 : count - 1 + number;
  return index >= 0 && index < count ? index : null;
}

export function allEditsSummary(turns: ChangeReviewTurn[]): FileEditSummaryData {
  const files = new Map<string, FileEditSummaryData["files"][number]>();
  for (const turn of turns) for (const file of turn.summary.files) {
    const previous = files.get(file.path);
    files.set(file.path, { ...file, diff: null, added: (previous?.added ?? 0) + file.added,
      removed: (previous?.removed ?? 0) + file.removed });
  }
  return { files: [...files.values()], added: turns.reduce((total, turn) => total + turn.summary.added, 0),
    removed: turns.reduce((total, turn) => total + turn.summary.removed, 0) };
}
