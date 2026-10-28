import type { Nodes } from "mdast";
import remarkCjkFriendly from "remark-cjk-friendly/parseOnly";
import remarkGfm from "remark-gfm";
import remarkParse from "remark-parse";
import { unified } from "unified";

const parser = unified().use(remarkParse).use(remarkGfm).use(remarkCjkFriendly);

function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]!);
}

interface MarkdownControls {
  code(source: string, language: string): string;
  table(html: string, source: string): string;
  image(url: string, alt: string): string;
}

// Parse complete blocks with Desktop's CommonMark/GFM and CJK rules. The panel
// supplies its existing copy/image controls; model output is always escaped.
export function renderMarkdown(source: string, controls: MarkdownControls): string {
  const tree = parser.parse(source);
  const definitions = new Map<string, { url: string; title?: string | null }>();
  for (const node of tree.children) {
    if (node.type === "definition") definitions.set(node.identifier.toLowerCase(), node);
  }
  const original = (node: Nodes) => source.slice(node.position?.start.offset, node.position?.end.offset);
  const children = (node: Nodes): string => "children" in node ? node.children.map(render).join("") : "";
  function link(url: string, label: string, title?: string | null): string {
    // Match the safe destinations usable from the chat Webview. Never turn
    // model-provided HTML or script URLs into executable markup.
    if (!/^(?:https?:\/\/|mailto:|#)/i.test(url)) return label;
    return '<a href="' + escapeHtml(url) + '"'
      + (title ? ' title="' + escapeHtml(title) + '"' : '')
      + ' rel="noopener noreferrer">' + label + '</a>';
  }
  function render(node: Nodes): string {
    switch (node.type) {
      case "root": return children(node);
      case "text": return escapeHtml(node.value);
      case "paragraph": return '<p class="md-text">' + children(node) + '</p>';
      case "heading": return '<h' + node.depth + ' class="md-text">' + children(node) + '</h' + node.depth + '>';
      case "strong": return '<strong>' + children(node) + '</strong>';
      case "emphasis": return '<em>' + children(node) + '</em>';
      case "delete": return '<del>' + children(node) + '</del>';
      case "inlineCode": return '<code>' + escapeHtml(node.value) + '</code>';
      case "code": return controls.code(node.value, node.lang || "");
      case "break": return '<br>';
      case "thematicBreak": return '<hr>';
      case "blockquote": return '<blockquote>' + children(node) + '</blockquote>';
      case "list": {
        const tag = node.ordered ? "ol" : "ul";
        const start = node.ordered && node.start != null ? ' start="' + node.start + '"' : '';
        return '<' + tag + start + '>' + children(node) + '</' + tag + '>';
      }
      case "listItem": {
        const checkbox = typeof node.checked === "boolean"
          ? '<input type="checkbox" disabled' + (node.checked ? ' checked' : '') + '> ' : '';
        return '<li' + (checkbox ? ' class="md-task-item"' : '') + '>' + checkbox + children(node) + '</li>';
      }
      case "link": return link(node.url, children(node), node.title);
      case "linkReference": {
        const definition = definitions.get(node.identifier.toLowerCase());
        return definition ? link(definition.url, children(node), definition.title) : escapeHtml(original(node));
      }
      case "image": return /^(?:https?:\/\/|data:image\/)/i.test(node.url)
        ? controls.image(node.url, node.alt || "") : escapeHtml(original(node));
      case "imageReference": {
        const definition = definitions.get(node.identifier.toLowerCase());
        return definition && /^(?:https?:\/\/|data:image\/)/i.test(definition.url)
          ? controls.image(definition.url, node.alt || "") : escapeHtml(original(node));
      }
      case "table": {
        const rows = node.children.map((row, index) => '<tr>' + row.children.map((cell, column) => {
          const tag = index === 0 ? 'th' : 'td';
          const align = node.align?.[column];
          return '<' + tag + (align ? ' style="text-align:' + align + '"' : '') + '>' + children(cell) + '</' + tag + '>';
        }).join('') + '</tr>');
        return controls.table('<table><thead>' + (rows[0] || '') + '</thead><tbody>' + rows.slice(1).join('') + '</tbody></table>', original(node));
      }
      case "definition": return '';
      case "html": return escapeHtml(node.value);
      default: return escapeHtml(original(node));
    }
  }
  return render(tree);
}
