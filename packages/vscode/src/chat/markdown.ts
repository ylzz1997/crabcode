import type { Nodes } from "mdast";
import remarkCjkFriendly from "remark-cjk-friendly/parseOnly";
import remarkParse from "remark-parse";
import { unified } from "unified";

const parser = unified().use(remarkParse).use(remarkCjkFriendly);

function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]!);
}

// Keep the chat panel's existing block layout, images and copy controls. Parse
// inline formatting with the same CJK rules as Desktop; never emit raw HTML.
export function renderInlineMarkdown(source: string): string {
  function renderChildren(nodes: Nodes[], start: number, end: number): string {
    let result = "";
    let offset = start;
    for (const node of nodes) {
      const nodeStart = node.position?.start.offset;
      const nodeEnd = node.position?.end.offset;
      if (nodeStart == null || nodeEnd == null) continue;
      result += escapeHtml(source.slice(offset, nodeStart));
      if (node.type === "text") {
        result += escapeHtml(node.value);
      } else if (node.type === "inlineCode") {
        result += "<code>" + escapeHtml(node.value) + "</code>";
      } else if (node.type === "strong" || node.type === "emphasis") {
        const tag = node.type === "strong" ? "strong" : "em";
        const markerLength = node.type === "strong" ? 2 : 1;
        result += "<" + tag + ">"
          + renderChildren(node.children, nodeStart + markerLength, nodeEnd - markerLength)
          + "</" + tag + ">";
      } else if ("children" in node) {
        // Preserve unsupported block/link syntax around any formatted children.
        result += renderChildren(node.children, nodeStart, nodeEnd);
      } else {
        result += escapeHtml(source.slice(nodeStart, nodeEnd));
      }
      offset = nodeEnd;
    }
    return result + escapeHtml(source.slice(offset, end));
  }

  return renderChildren(parser.parse(source).children, 0, source.length);
}
