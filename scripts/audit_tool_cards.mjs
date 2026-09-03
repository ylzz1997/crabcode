import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = dirname(dirname(fileURLToPath(import.meta.url)));

function pythonSources(directory) {
  return readdirSync(directory)
    .filter((name) => name.endsWith(".py"))
    .map((name) => readFileSync(join(directory, name), "utf8"));
}

function registryNames(source, pattern, label) {
  const block = source.match(pattern);
  if (!block) throw new Error(`${label} tool presentation registry was not found`);
  return new Set(Array.from(
    block[1].matchAll(/\b([a-z][a-z0-9_]*)\s*:\s*(?:\{|\[)/g),
    (match) => match[1],
  ));
}

const sources = [
  ...pythonSources(join(repositoryRoot, "packages/core/crabcode_core/tools")),
  readFileSync(join(repositoryRoot, "packages/search/crabcode_search/tool.py"), "utf8"),
  readFileSync(join(repositoryRoot, "packages/debugger/crabcode_debugger/tool.py"), "utf8"),
];
const registeredNames = Array.from(new Set(sources.flatMap((source) => (
  Array.from(source.matchAll(/^\s{4}name\s*=\s*["']([^"']+)["']/gm), (match) => match[1])
)))).sort();
const normalize = (value) => value.trim().toLowerCase().replace(/[\s.-]/g, "");

const desktopSource = readFileSync(join(repositoryRoot, "packages/desktop/src/toolPresentation.ts"), "utf8");
const desktopNames = registryNames(
  desktopSource,
  /const DEFINITIONS:[^=]+\s= \{([\s\S]*?)\r?\n\};\r?\n\r?\nconst FIELD_LABELS/,
  "Desktop",
);

const chatPanelPath = join(repositoryRoot, "packages/vscode/src/chatPanel.ts");
const chatPanel = readFileSync(chatPanelPath, "utf8");
const vscodeNames = registryNames(
  chatPanel,
  /const TOOL_PRESENTATIONS = \{([\s\S]*?)\r?\n    \};/,
  "VS Code",
);

for (const [label, names] of [["Desktop", desktopNames], ["VS Code", vscodeNames]]) {
  const missing = registeredNames.filter((name) => !names.has(normalize(name)));
  if (missing.length) throw new Error(`${label} is missing tool card presentations: ${missing.join(", ")}`);
}

// TypeScript does not parse JavaScript embedded in the generated Webview HTML.
// Evaluate the template with a harmless nonce, then syntax-check its script.
const functionStart = chatPanel.indexOf("  private getHtmlForWebview(");
const literalStart = chatPanel.indexOf("`<!DOCTYPE html>", functionStart);
const literalEnd = chatPanel.indexOf("</html>`;", literalStart) + "</html>`".length;
if (functionStart < 0 || literalStart < 0 || literalEnd < "</html>`".length) {
  throw new Error("VS Code Webview template was not found");
}
const templateLiteral = chatPanel.slice(literalStart, literalEnd);
const html = Function("nonce", `return ${templateLiteral}`)("audit-nonce");
const webviewScript = html.match(/<script[^>]*>([\s\S]*?)<\/script>/)?.[1];
if (!webviewScript) throw new Error("VS Code Webview script was not found");
Function(webviewScript);

console.log(`Tool card audit passed for Desktop and VS Code (${registeredNames.length} registered tools).`);
