import { describe, expect, it } from "vitest";
import {
  clampDocumentAgentWidth,
  clampDocumentZoom,
  documentTranslationToggleLabel,
  hasDocumentTranslationCache,
  formatDocumentZoom,
  parseDocumentPageInput,
  parseDocumentZoomInput,
  documentZoomDeltaForKeyboardEvent,
  documentZoomFromPinchWheel,
  documentSelectionLineRange,
  groupTextBlocksIntoParagraphs,
  looksLikeFormulaText,
  translatedBox,
  unrotateSelectionBox,
} from "./DocumentWorkspace";
import type { DocumentManifest, DocumentTextBlock } from "./types";

const block: DocumentTextBlock = {
  id: "p1-b0",
  text: "Hello",
  x: .1,
  y: .2,
  width: .3,
  height: .1,
  fontSize: 12,
  fontFamily: "sans-serif",
  direction: "ltr",
};

describe("document Agent panel sizing", () => {
  it("uses the available panel width instead of a fixed maximum", () => {
    expect(clampDocumentAgentWidth(1_200, 1_600)).toBe(1_200);
    expect(clampDocumentAgentWidth(1_500, 1_600)).toBe(1_420);
    expect(clampDocumentAgentWidth(200, 1_600)).toBe(320);
  });
});

function textBlock(
  id: string,
  text: string,
  x: number,
  y: number,
  width: number,
  height = .012,
  fontSize = 10,
): DocumentTextBlock {
  return {
    id,
    text,
    x,
    y,
    width,
    height,
    fontSize,
    fontFamily: "sans-serif",
    direction: "ltr",
  };
}

describe("document translation coordinates", () => {
  const manifest = (overrides: Partial<DocumentManifest> = {}): DocumentManifest => ({
    schema_version: 1,
    project_id: "project-1",
    project_name: "Test",
    workspace: "/test",
    source: {
      origin: "upload",
      name: "test.pdf",
      path: "test.pdf",
      url: null,
      content_type: "application/pdf",
      size: 1,
      sha256: "source",
    },
    pdf: { path: "test.pdf", sha256: "pdf", page_count: 1 },
    layout: null,
    translations: {},
    blog: null,
    jobs: {},
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  });

  it("only enables translation cache clearing when the selected locale has cache or progress", () => {
    expect(hasDocumentTranslationCache(null, "zh-CN")).toBe(false);
    expect(hasDocumentTranslationCache(manifest(), "zh-CN")).toBe(false);
    expect(hasDocumentTranslationCache(manifest({
      translations: { "zh-CN": { engine: "legacy", path: ".crabcode/document/translations/zh-CN.json" } },
    }), "zh-CN")).toBe(true);
    expect(hasDocumentTranslationCache(manifest({
      translations: { en: { engine: "legacy", path: ".crabcode/document/translations/en.json" } },
    }), "zh-CN")).toBe(false);
    expect(hasDocumentTranslationCache(manifest({
      jobs: {
        "translate-1": {
          action: "translate",
          status: "failed",
          locale: "zh-CN",
          source: "original",
          current: 2,
          total: 10,
          message: "failed",
          updated_at: "2026-01-01T00:00:00Z",
        },
      },
    }), "zh-CN")).toBe(true);
  });

  it("maps a PDF selection to stable document-wide line numbers", () => {
    const result = documentSelectionLineRange({
      fingerprint: "test",
      page_count: 2,
      pages: [
        {
          width: 100,
          height: 100,
          blocks: [],
          lines: [
            { x: .1, y: .1, width: .8, height: .05 },
            { x: .1, y: .2, width: .8, height: .05 },
          ],
        },
        {
          width: 100,
          height: 100,
          blocks: [],
          lines: [
            { x: .1, y: .1, width: .8, height: .05 },
            { x: .1, y: .2, width: .8, height: .05 },
            { x: .1, y: .3, width: .8, height: .05 },
          ],
        },
      ],
    }, [
      { page: 2, x: .2, y: .21, width: .3, height: .02 },
      { page: 2, x: .2, y: .31, width: .3, height: .02 },
    ]);

    expect(result).toEqual({ start: 4, end: 5 });
  });

  it("labels the PDF toggle with the action it will perform", () => {
    expect(documentTranslationToggleLabel(false)).toBe("查看译文");
    expect(documentTranslationToggleLabel(true)).toBe("查看原文");
  });

  it("keeps zoom in the same range for buttons and keyboard shortcuts", () => {
    expect(clampDocumentZoom(.1)).toBe(.6);
    expect(clampDocumentZoom(1.24)).toBe(1.24);
    expect(clampDocumentZoom(1.23456)).toBe(1.23);
    expect(clampDocumentZoom(2.9)).toBe(2.5);
  });

  it("formats and parses editable page and zoom values", () => {
    expect(formatDocumentZoom(1.4)).toBe("140%");
    expect(formatDocumentZoom(1.374)).toBe("137%");
    expect(parseDocumentZoomInput("137.4%", 1.2)).toBe(1.37);
    expect(parseDocumentZoomInput("900", 1.2)).toBe(2.5);
    expect(parseDocumentZoomInput("invalid", 1.2)).toBe(1.2);
    expect(parseDocumentPageInput("12", 25, 2)).toBe(12);
    expect(parseDocumentPageInput("90", 25, 2)).toBe(25);
    expect(parseDocumentPageInput("invalid", 25, 2)).toBe(2);
  });

  it("uses physical minus and equal keys for macOS Option shortcuts", () => {
    const shortcut = (code: string, overrides = {}) => documentZoomDeltaForKeyboardEvent({
      altKey: true,
      ctrlKey: false,
      metaKey: false,
      code,
      ...overrides,
    });
    expect(shortcut("Equal")).toBe(.1);
    expect(shortcut("Minus")).toBe(-.1);
    expect(shortcut("Equal", { altKey: false })).toBe(0);
    expect(shortcut("Minus", { metaKey: true })).toBe(0);
  });

  it("accumulates smooth trackpad pinch deltas in the expected direction", () => {
    let targetZoom = 1.2;
    for (let index = 0; index < 6; index += 1) {
      targetZoom = documentZoomFromPinchWheel(targetZoom, -1);
    }
    expect(targetZoom).toBeGreaterThan(1.2);
    expect(clampDocumentZoom(targetZoom)).toBe(1.27);

    expect(documentZoomFromPinchWheel(1.2, 10)).toBeLessThan(1.2);
    expect(documentZoomFromPinchWheel(2.5, -10)).toBe(2.5);
    expect(documentZoomFromPinchWheel(.6, 10)).toBe(.6);
  });

  it("keeps normalized boxes anchored through page rotations", () => {
    const expected = [
      [0, .1, .2, .3, .1],
      [90, .7, .1, .1, .3],
      [180, .6, .7, .3, .1],
      [270, .2, .6, .1, .3],
    ];
    for (const [rotation, x, y, width, height] of expected) {
      const result = translatedBox(block, rotation);
      expect(result.x).toBeCloseTo(x);
      expect(result.y).toBeCloseTo(y);
      expect(result.width).toBeCloseTo(width);
      expect(result.height).toBeCloseTo(height);
    }
  });

  it("converts selected display rectangles back to stable page coordinates", () => {
    for (const rotation of [0, 90, 180, 270]) {
      const displayed = translatedBox(block, rotation);
      const result = unrotateSelectionBox(displayed, rotation);
      expect(result.x).toBeCloseTo(block.x);
      expect(result.y).toBeCloseTo(block.y);
      expect(result.width).toBeCloseTo(block.width);
      expect(result.height).toBeCloseTo(block.height);
    }
  });

  it("rebuilds fragmented PDF text into centered titles and body paragraphs", () => {
    const grouped = groupTextBlocksIntoParagraphs([
      textBlock("title-1", "F", .30, .10, .015, .020, 16),
      textBlock("title-2", "OURIER", .316, .104, .10, .016, 13),
      textBlock("title-3", "T", .424, .10, .015, .020, 16),
      textBlock("title-4", "RANSFORM", .44, .104, .14, .016, 13),
      textBlock("title-5", "FOR", .38, .125, .05, .016, 13),
      textBlock("title-6", "PDF", .44, .125, .06, .016, 13),
      textBlock("body-1", "A translated paragraph starts", .12, .30, .72),
      textBlock("body-2", "on one line and continues on the", .12, .313, .72),
      textBlock("body-3", "next line.", .12, .326, .18),
      textBlock("body-4", "A new paragraph starts here.", .12, .339, .35),
    ], 1);

    expect(grouped).toHaveLength(3);
    expect(grouped[0]).toMatchObject({
      text: "FOURIER TRANSFORM FOR PDF",
      textAlign: "center",
    });
    expect(grouped[1].text).toBe("A translated paragraph starts on one line and continues on the next line.");
    expect(grouped[2].text).toBe("A new paragraph starts here.");
  });

  it("keeps horizontally separated columns in different paragraphs", () => {
    const grouped = groupTextBlocksIntoParagraphs([
      textBlock("left-1", "Left author", .12, .20, .15),
      textBlock("right-1", "Right author", .62, .20, .16),
      textBlock("left-2", "Left affiliation", .10, .214, .19),
      textBlock("right-2", "Right affiliation", .60, .214, .20),
    ], 1);

    expect(grouped).toHaveLength(2);
    expect(grouped.map((item) => item.text)).toEqual([
      "Left author Left affiliation",
      "Right author Right affiliation",
    ]);
  });

  it("preserves line-end hyphens and spaces around relation symbols", () => {
    const grouped = groupTextBlocksIntoParagraphs([
      textBlock("line-1", "These shortcomings in high-", .12, .30, .31),
      textBlock("line-2", "fidelity generation remain.", .12, .313, .28),
      textBlock("line-3a", "Allocation uses", .12, .326, .12),
      textBlock("line-3b", "≈", .2405, .326, .01),
      textBlock("line-3c", "45% fewer dimensions.", .2508, .326, .20),
    ], 1);

    expect(grouped).toHaveLength(1);
    expect(grouped[0].text).toBe(
      "These shortcomings in high-fidelity generation remain. Allocation uses ≈45% fewer dimensions.",
    );
  });

  it("keeps a raised footnote marker with its text", () => {
    const grouped = groupTextBlocksIntoParagraphs([
      textBlock("marker", "∗", .1370, .8967, .0062, .0075, 7),
      textBlock("footnote", "Corresponding author.", .1440, .8978, .1310, .0113, 9),
    ], 1);

    expect(grouped).toHaveLength(1);
    expect(grouped[0].text).toBe("∗ Corresponding author.");
  });

  it("keeps display formulas separate from surrounding prose", () => {
    expect(looksLikeFormulaText("Spec-SnakeBeta(x)b,c,t,f = xb,c,t,f +")).toBe(true);
    expect(looksLikeFormulaText("sin")).toBe(true);
    expect(looksLikeFormulaText("(1)")).toBe(false);
    expect(looksLikeFormulaText("The model is initialized in log space.")).toBe(false);

    const grouped = groupTextBlocksIntoParagraphs([
      textBlock("prose-1", "The parameters are initialized as follows:", .12, .30, .38),
      textBlock("formula-1", "Spec-SnakeBeta(x)b,c,t,f = xb,c,t,f +", .22, .33, .32),
      textBlock("formula-2", "sin", .60, .33, .025),
      textBlock("formula-number", "(1)", .84, .33, .025),
      textBlock("formula-subscript", "ℓ,f", .55, .341, .02, .007, 7),
      textBlock("formula-symbol", "Fℓ", .48, .342, .02),
      textBlock("prose-2", "We optimize both parameters in log space.", .12, .37, .40),
    ], 1);

    expect(grouped.filter((item) => item.kind === "formula").map((item) => item.text)).toEqual([
      "Spec-SnakeBeta(x)b,c,t,f = xb,c,t,f +",
      "sin",
      "(1)",
      "ℓ,f",
      "Fℓ",
    ]);
    expect(grouped.filter((item) => item.kind === "text").map((item) => item.text)).toEqual([
      "The parameters are initialized as follows:",
      "We optimize both parameters in log space.",
    ]);
  });

  it("leaves dense figure labels on the original PDF canvas", () => {
    const grouped = groupTextBlocksIntoParagraphs([
      textBlock("body", "The architecture is shown in the following figure.", .12, .55, .72),
      textBlock("label-1", "Encoder", .18, .18, .08, .008, 7),
      textBlock("label-2", "Decoder", .30, .20, .08, .008, 7),
      textBlock("label-3", "High Freq", .42, .22, .09, .008, 7),
      textBlock("label-4", "Mid Freq", .54, .24, .09, .008, 7),
      textBlock("label-5", "Low Freq", .66, .26, .09, .008, 7),
    ], 1);

    expect(grouped.filter((item) => item.kind === "graphic")).toHaveLength(5);
    expect(grouped.filter((item) => item.kind === "text").map((item) => item.text)).toEqual([
      "The architecture is shown in the following figure.",
    ]);
  });
});
