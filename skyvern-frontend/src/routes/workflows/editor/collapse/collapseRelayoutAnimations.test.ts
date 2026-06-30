import { describe, expect, test } from "vitest";

import {
  HEIGHT_COLLAPSE_ANIMATION_NAMES,
  isHeightCollapseAnimation,
} from "./collapseRelayoutAnimations";

describe("isHeightCollapseAnimation", () => {
  test("matches every height-animating collapse/accordion keyframe", () => {
    for (const name of [
      "accordion-down",
      "accordion-up",
      "collapsible-down",
      "collapsible-up",
      "collapsible-down-fade",
      "collapsible-up-fade",
    ]) {
      expect(isHeightCollapseAnimation(name)).toBe(true);
    }
  });

  test("ignores unrelated animations (no spurious relayout)", () => {
    for (const name of ["glow", "fade-in", "spin", "", "accordion"]) {
      expect(isHeightCollapseAnimation(name)).toBe(false);
    }
  });

  test("the name set stays aligned with the keyframes it gates", () => {
    expect(HEIGHT_COLLAPSE_ANIMATION_NAMES.size).toBe(6);
  });
});
