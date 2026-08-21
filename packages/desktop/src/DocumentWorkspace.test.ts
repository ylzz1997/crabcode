import { describe, expect, it } from "vitest";
import { clampDocumentZoom, groupTextBlocksIntoParagraphs, translatedBox } from "./DocumentWorkspace";
import type { DocumentTextBlock } from "./types";

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
  it("keeps zoom in the same range for buttons and keyboard shortcuts", () => {
    expect(clampDocumentZoom(.1)).toBe(.6);
    expect(clampDocumentZoom(1.24)).toBe(1.2);
    expect(clampDocumentZoom(2.9)).toBe(2.5);
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
});
