export interface DiffRow {
  kind: "gap" | "add" | "remove" | "context";
  text: string;
  oldNumber: number | null;
  newNumber: number | null;
  gap: number;
}

export function diffRows(diff: string | null | undefined): DiffRow[] {
  if (!diff) return [];
  const lines = diff.split("\n");
  const rows: DiffRow[] = [];
  let index = 0;
  let previousEnd = 0;
  while (index < lines.length) {
    const header = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/.exec(lines[index] ?? "");
    if (!header) {
      index += 1;
      continue;
    }
    const oldStart = Number(header[1]);
    const newStart = Number(header[3]);
    let oldRemaining = Number(header[2] ?? 1);
    let newRemaining = Number(header[4] ?? 1);
    const gap = oldStart - previousEnd - 1;
    if (gap > 0) rows.push({ kind: "gap", text: "", oldNumber: null, newNumber: null, gap });
    index += 1;
    let oldLine = oldStart;
    let newLine = newStart;
    while (index < lines.length && (oldRemaining > 0 || newRemaining > 0) && !(lines[index] ?? "").startsWith("@@")) {
      const line = lines[index] ?? "";
      if (line.startsWith("+")) {
        rows.push({ kind: "add", text: line.slice(1), oldNumber: null, newNumber: newLine, gap: 0 });
        newLine += 1;
        newRemaining -= 1;
      } else if (line.startsWith("-")) {
        rows.push({ kind: "remove", text: line.slice(1), oldNumber: oldLine, newNumber: null, gap: 0 });
        oldLine += 1;
        oldRemaining -= 1;
      } else if (line.startsWith("\\")) {
        index += 1;
        continue;
      } else if (line.startsWith(" ")) {
        const text = line.slice(1);
        rows.push({ kind: "context", text, oldNumber: oldLine, newNumber: newLine, gap: 0 });
        oldLine += 1;
        newLine += 1;
        oldRemaining -= 1;
        newRemaining -= 1;
      } else break;
      index += 1;
    }
    previousEnd = oldLine - 1;
  }
  return rows;
}

export function reviewTurnLabel(index: number, total: number): string {
  // The list contains only file-edit summaries, not every conversation turn.
  // Keep historical entries stable when another edit summary is appended.
  return index === total - 1 ? "最近一次编辑" : `第 ${index + 1} 次编辑`;
}
