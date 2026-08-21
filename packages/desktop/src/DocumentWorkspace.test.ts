import { describe, expect, it } from "vitest";
import { translatedBox } from "./DocumentWorkspace";
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

describe("document translation coordinates", () => {
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
});
