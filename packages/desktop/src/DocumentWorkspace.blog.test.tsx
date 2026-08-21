/* @vitest-environment jsdom */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { BlogPreview } from "./DocumentWorkspace";
import type { GatewayApi } from "./gateway";
import { normalizeMarkdownMathDelimiters } from "./markdownMath";

describe("document Blog math", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it("renders dollar and LaTeX-style math delimiters with KaTeX", () => {
    const markdown = String.raw`Inline $y$ and \(x_t\), followed by:

\[
s_\theta(x_t, y, t) \approx \nabla_{x_t} \log p_t(x_t \mid y)
\]`;

    act(() => root.render(
      <BlogPreview api={{} as GatewayApi} workspace="/test" markdown={markdown} />,
    ));

    expect(container.querySelectorAll(".katex")).toHaveLength(3);
    expect(container.querySelector(".katex-display")).not.toBeNull();
    expect(container.querySelector(".katex-display")?.textContent).toContain("sθ");
  });

  it("does not rewrite math-like delimiters inside code", () => {
    const markdown = [
      "Inline code: `\\(literal\\)`",
      "",
      "```tex",
      "\\[literal\\]",
      "```",
    ].join("\n");

    expect(normalizeMarkdownMathDelimiters(markdown)).toBe(markdown);
  });

  it("leaves unmatched escaped punctuation as regular Markdown", () => {
    const markdown = String.raw`Keep \( and \] when they are not math delimiter pairs.`;

    expect(normalizeMarkdownMathDelimiters(markdown)).toBe(markdown);
  });
});
