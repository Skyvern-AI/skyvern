import { fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import {
  DEFAULT_SPLIT_FRACTION,
  TIMELINE_SPLIT_STORAGE_KEY,
} from "./resizableTimelineSplitMath";
import { ResizableTimelineSplit } from "./ResizableTimelineSplit";

describe("ResizableTimelineSplit", () => {
  beforeEach(() => {
    localStorage.clear();
  });
  afterEach(() => {
    localStorage.clear();
  });

  function renderSplit() {
    return render(
      <ResizableTimelineSplit
        top={<div>top pane</div>}
        bottom={<div>bottom pane</div>}
      />,
    );
  }

  test("defaults to an even split with no persisted value", () => {
    const { container } = renderSplit();
    const divider = container.querySelector('[role="separator"]')!;
    expect(divider.getAttribute("aria-orientation")).toBe("horizontal");
    expect(divider.getAttribute("tabindex")).toBe("0");
    expect(divider.getAttribute("aria-valuenow")).toBe("50");
    expect(divider.getAttribute("aria-valuemin")).toBe("0");
    expect(divider.getAttribute("aria-valuemax")).toBe("100");
  });

  test("restores a previously persisted split", () => {
    localStorage.setItem(TIMELINE_SPLIT_STORAGE_KEY, JSON.stringify(0.3));
    const { container } = renderSplit();
    const divider = container.querySelector('[role="separator"]')!;
    expect(divider.getAttribute("aria-valuenow")).toBe("30");
  });

  test("sanitizes a corrupted persisted value back to the default", () => {
    localStorage.setItem(TIMELINE_SPLIT_STORAGE_KEY, "not-json");
    const { container } = renderSplit();
    const divider = container.querySelector('[role="separator"]')!;
    expect(divider.getAttribute("aria-valuenow")).toBe("50");
  });

  test("is invisible at rest", () => {
    const { container } = renderSplit();
    const indicator = container.querySelector("span[aria-hidden]")!;
    expect(indicator.className).toContain("bg-transparent");
    expect(indicator.className).not.toContain("bg-muted-foreground");
  });

  test("double-click resets to the default split and persists it", () => {
    localStorage.setItem(TIMELINE_SPLIT_STORAGE_KEY, JSON.stringify(0.25));
    const { container } = renderSplit();
    const divider = container.querySelector('[role="separator"]')!;
    fireEvent.doubleClick(divider);
    expect(divider.getAttribute("aria-valuenow")).toBe("50");
    expect(localStorage.getItem(TIMELINE_SPLIT_STORAGE_KEY)).toBe(
      JSON.stringify(DEFAULT_SPLIT_FRACTION),
    );
  });

  // jsdom has no layout engine, so getBoundingClientRect() is always a zero
  // rect — stubbing it to a fixed size is the only way to exercise the
  // keyboard-nudge wiring here. It pins the ArrowUp/ArrowDown contract; it
  // cannot stand in for the real pointer-drag geometry check (see the
  // Playwright harness referenced in the PR description).
  describe("keyboard nudge (stubbed layout)", () => {
    const CONTAINER_HEIGHT = 400;

    beforeEach(() => {
      vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
        height: CONTAINER_HEIGHT,
        width: 0,
        top: 0,
        left: 0,
        bottom: CONTAINER_HEIGHT,
        right: 0,
        x: 0,
        y: 0,
        toJSON() {
          return this;
        },
      });
    });

    afterEach(() => {
      vi.restoreAllMocks();
    });

    test("ArrowDown grows the top pane by one step and persists it", () => {
      const { container } = renderSplit();
      const divider = container.querySelector('[role="separator"]')!;
      fireEvent.keyDown(divider, { key: "ArrowDown" });
      // contentHeight = 400 - 12 (divider) = 388; 50% + 24px = 218/388 ≈ 56%.
      expect(divider.getAttribute("aria-valuenow")).toBe("56");
      expect(localStorage.getItem(TIMELINE_SPLIT_STORAGE_KEY)).toBe(
        JSON.stringify(218 / 388),
      );
    });

    test("ArrowUp shrinks the top pane by one step", () => {
      const { container } = renderSplit();
      const divider = container.querySelector('[role="separator"]')!;
      fireEvent.keyDown(divider, { key: "ArrowUp" });
      expect(divider.getAttribute("aria-valuenow")).toBe("44");
    });

    test("ignores keys other than the arrow keys", () => {
      const { container } = renderSplit();
      const divider = container.querySelector('[role="separator"]')!;
      fireEvent.keyDown(divider, { key: "Enter" });
      expect(divider.getAttribute("aria-valuenow")).toBe("50");
      expect(localStorage.getItem(TIMELINE_SPLIT_STORAGE_KEY)).toBeNull();
    });

    test("a second pointer's up/cancel does not end a different pointer's drag", () => {
      if (!Element.prototype.setPointerCapture) {
        Element.prototype.setPointerCapture = vi.fn();
      }
      const { container } = renderSplit();
      const divider = container.querySelector('[role="separator"]')!;
      fireEvent.pointerDown(divider, { pointerId: 1, clientY: 200, button: 0 });
      fireEvent.pointerMove(divider, { pointerId: 1, clientY: 230 });
      // A stray second pointer (e.g. an incidental touch) ending on the same
      // element must not terminate pointer 1's still-active drag.
      fireEvent.pointerUp(divider, { pointerId: 2 });
      fireEvent.pointerMove(divider, { pointerId: 1, clientY: 250 });
      fireEvent.pointerUp(divider, { pointerId: 1 });
      // contentHeight=388, startTopPx=194, +50px move -> 244/388 (well clear
      // of the [120, 268] clamp bounds either move would land in).
      expect(localStorage.getItem(TIMELINE_SPLIT_STORAGE_KEY)).toBe(
        JSON.stringify(244 / 388),
      );
    });
  });
});
