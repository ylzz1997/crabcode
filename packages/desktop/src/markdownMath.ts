function normalizeMathText(value: string): string {
  const normalized = value
    .replace(/(?<!\\)\\\[([\s\S]+?)(?<!\\)\\\]/g, (_match, expression: string) => {
      const content = expression.trim();
      return `\n$$\n${content}\n$$\n`;
    })
    .replace(/(?<!\\)\\\(([^\n]+?)(?<!\\)\\\)/g, (_match, expression: string) => `$${expression}$`);

  const lines = normalized.split("\n");
  const result: string[] = [];
  let displayMath = false;
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (/^\s*\$\$\s*$/.test(line)) {
      displayMath = !displayMath;
      result.push(line);
      continue;
    }
    const opening = !displayMath && line.match(/^\s*\\begin\{(equation\*?|align\*?)\}\s*$/);
    if (!opening) {
      result.push(line);
      continue;
    }
    const environment = opening[1];
    const endPattern = new RegExp(`^\\s*\\\\end\\{${environment.replace("*", "\\*")}\\}\\s*$`);
    let end = index + 1;
    while (end < lines.length && !endPattern.test(lines[end])) end += 1;
    if (end >= lines.length) {
      result.push(line);
      continue;
    }
    const content = lines.slice(index + 1, end).join("\n").trim();
    const body = environment.startsWith("align")
      ? `\\begin{aligned}\n${content}\n\\end{aligned}`
      : content;
    result.push("$$", body, "$$");
    index = end;
  }
  return result.join("\n");
}

function normalizeMathOutsideInlineCode(value: string): string {
  let result = "";
  let index = 0;
  let segmentStart = 0;
  while (index < value.length) {
    if (value[index] !== "`") {
      index += 1;
      continue;
    }

    let openingEnd = index + 1;
    while (value[openingEnd] === "`") openingEnd += 1;
    const ticks = openingEnd - index;
    let closingStart = openingEnd;
    let closingEnd = -1;
    while (closingStart < value.length) {
      if (value[closingStart] !== "`") {
        closingStart += 1;
        continue;
      }
      let candidateEnd = closingStart + 1;
      while (value[candidateEnd] === "`") candidateEnd += 1;
      if (candidateEnd - closingStart === ticks) {
        closingEnd = candidateEnd;
        break;
      }
      closingStart = candidateEnd;
    }

    result += normalizeMathText(value.slice(segmentStart, index));
    if (closingEnd < 0) return result + value.slice(index);
    result += value.slice(index, closingEnd);
    index = closingEnd;
    segmentStart = closingEnd;
  }
  return result + normalizeMathText(value.slice(segmentStart));
}

export function normalizeMarkdownMathDelimiters(markdown: string): string {
  const lines = markdown.split("\n");
  const codeLines = new Set<number>();
  let fence: { marker: string; length: number } | null = null;

  lines.forEach((line, index) => {
    const match = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (fence) codeLines.add(index);
    if (!match) return;
    if (!fence) {
      fence = { marker: match[1][0], length: match[1].length };
      codeLines.add(index);
    } else if (
      match[1][0] === fence.marker
      && match[1].length >= fence.length
      && !match[2].trim()
    ) {
      fence = null;
    }
  });

  const chunks: string[] = [];
  let start = 0;
  while (start < lines.length) {
    const code = codeLines.has(start);
    let end = start + 1;
    while (end < lines.length && codeLines.has(end) === code) end += 1;
    const chunk = lines.slice(start, end).join("\n");
    chunks.push(code ? chunk : normalizeMathOutsideInlineCode(chunk));
    start = end;
  }
  return chunks.join("\n");
}
