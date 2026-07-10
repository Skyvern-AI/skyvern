import { describe, expect, test } from "vitest";

import {
  clampSplitFraction,
  DEFAULT_SPLIT_FRACTION,
  gridTemplateRowsFor,
  MIN_PANE_HEIGHT_PX,
  sanitizeSplitFraction,
} from "./resizableTimelineSplitMath";

describe("sanitizeSplitFraction", () => {
  test("passes through a valid fraction", () => {
    expect(sanitizeSplitFraction(0.3)).toBe(0.3);
  });

  test("falls back to the default for out-of-range or malformed values", () => {
    for (const value of [0, 1, -0.2, 1.2, NaN, "0.5", null, undefined, {}]) {
      expect(sanitizeSplitFraction(value)).toBe(DEFAULT_SPLIT_FRACTION);
    }
  });
});

describe("clampSplitFraction", () => {
  test("passes through a mid-range value untouched", () => {
    expect(clampSplitFraction(500, 1000)).toBeCloseTo(0.5);
  });

  test("clamps below the top floor", () => {
    expect(clampSplitFraction(10, 1000)).toBeCloseTo(MIN_PANE_HEIGHT_PX / 1000);
  });

  test("clamps above the bottom floor", () => {
    expect(clampSplitFraction(990, 1000)).toBeCloseTo(
      (1000 - MIN_PANE_HEIGHT_PX) / 1000,
    );
  });

  test("falls back to 50/50 when the container can't fit both floors", () => {
    expect(clampSplitFraction(100, MIN_PANE_HEIGHT_PX * 2)).toBe(
      DEFAULT_SPLIT_FRACTION,
    );
    expect(clampSplitFraction(100, MIN_PANE_HEIGHT_PX * 2 - 1)).toBe(
      DEFAULT_SPLIT_FRACTION,
    );
  });

  test("degrades proportionally just above that boundary, never overflowing", () => {
    const contentHeight = MIN_PANE_HEIGHT_PX * 2 + 1;
    const result = clampSplitFraction(MIN_PANE_HEIGHT_PX, contentHeight);
    expect(result).toBeCloseTo(MIN_PANE_HEIGHT_PX / contentHeight);
    expect(result).toBeGreaterThan(0);
    expect(result).toBeLessThan(1);
  });
});

describe("gridTemplateRowsFor", () => {
  test("emitted fr factors always sum to the row scale, even at clamp bounds", () => {
    const fractions = [0, 0.001, 0.1, 1 / 3, 0.5, 0.6, 0.9, 0.999, 1];
    for (const fraction of fractions) {
      const template = gridTemplateRowsFor(fraction);
      const match = template.match(
        /^minmax\(0, (\d+)fr\) 12px minmax\(0, (\d+)fr\)$/,
      );
      expect(match).not.toBeNull();
      const [, top, bottom] = match!;
      expect(Number(top) + Number(bottom)).toBe(1000);
    }
  });
});
