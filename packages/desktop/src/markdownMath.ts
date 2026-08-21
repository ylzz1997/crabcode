function normalizeMathText(value: string): string {
  return value
    .replace(/(?<!\\)\\\((.+?)(?<!\\)\\\)/g, (_match, expression: string) => `$${expression}$`)
    .replace(/(?<!\\)\\\[(.+?)(?<!\\)\\\]/g, (_match, expression: string) => `\n$$\n${expression}\n$$\n`);
}

function normalizeMathDelimitersInLine(line: string): string {
  let result = "";
  let index = 0;
  let codeTicks = 0;
  let segmentStart = 0;
  while (index < line.length) {
    if (line[index] === "`") {
      let end = index + 1;
      while (line[end] === "`") end += 1;
      const ticks = end - index;
      if (codeTicks === 0) {
        result += normalizeMathText(line.slice(segmentStart, index));
        result += line.slice(index, end);
        codeTicks = ticks;
        segmentStart = end;
      } else if (codeTicks === ticks) {
        result += line.slice(segmentStart, end);
        codeTicks = 0;
        segmentStart = end;
      }
      index = end;
      continue;
    }
    index += 1;
  }
  return result + (codeTicks === 0
    ? normalizeMathText(line.slice(segmentStart))
    : line.slice(segmentStart));
}

export function normalizeMarkdownMathDelimiters(markdown: string): string {
  let fence: { marker: string; length: number } | null = null;
  return markdown.split("\n").map((line) => {
    const match = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (match) {
      const marker = match[1][0];
      if (!fence) fence = { marker, length: match[1].length };
      else if (
        marker === fence.marker
        && match[1].length >= fence.length
        && !match[2].trim()
      ) fence = null;
      return line;
    }
    if (fence) return line;
    if (/^\s*\\\[\s*$/.test(line)) return line.replace("\\[", () => "$$");
    if (/^\s*\\\]\s*$/.test(line)) return line.replace("\\]", () => "$$");
    return normalizeMathDelimitersInLine(line);
  }).join("\n");
}
