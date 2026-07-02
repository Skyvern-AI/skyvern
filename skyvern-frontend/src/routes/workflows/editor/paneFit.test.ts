import { describe, expect, test } from "vitest";

import {
  isMeaningfulPaneResize,
  isViewportStranded,
  PANE_RESIZE_EPSILON_PX,
} from "./paneFit";

describe("isMeaningfulPaneResize", () => {
  test("ignores sub-epsilon jitter on both axes", () => {
    expect(
      isMeaningfulPaneResize(
        { width: 500, height: 800 },
        { width: 500 + PANE_RESIZE_EPSILON_PX - 1, height: 801 },
      ),
    ).toBe(false);
  });

  test("detects a pane-toggle sized width change", () => {
    expect(
      isMeaningfulPaneResize(
        { width: 1000, height: 800 },
        { width: 330, height: 800 },
      ),
    ).toBe(true);
  });

  test("detects a height-only change", () => {
    expect(
      isMeaningfulPaneResize(
        { width: 500, height: 800 },
        { width: 500, height: 600 },
      ),
    ).toBe(true);
  });
});

describe("isViewportStranded", () => {
  const chainBounds = { x: 0, y: 0, width: 500, height: 1200 };

  test("a freshly fitted viewport is not stranded", () => {
    // zoom 0.6 centers the 500-wide chain in a 330-wide pane.
    expect(
      isViewportStranded({
        pane: { width: 330, height: 800 },
        viewport: { x: 15, y: 20, zoom: 0.6 },
        bounds: chainBounds,
      }),
    ).toBe(false);
  });

  test("a pane shrink that leaves only a sliver of the chain is stranded", () => {
    // Viewport fitted for a 1000-wide pane (chain centered at x=250, zoom 1),
    // then the pane shrank to 330: only ~80px of the chain remains visible.
    expect(
      isViewportStranded({
        pane: { width: 330, height: 800 },
        viewport: { x: 250, y: 0, zoom: 1 },
        bounds: chainBounds,
      }),
    ).toBe(true);
  });

  test("a viewport panned fully off the flow is stranded", () => {
    expect(
      isViewportStranded({
        pane: { width: 500, height: 800 },
        viewport: { x: -2000, y: 0, zoom: 1 },
        bounds: chainBounds,
      }),
    ).toBe(true);
  });

  test("a deliberate zoom into one block keeps the viewport", () => {
    // Block region fills the pane: tiny share of the whole flow visible, but
    // the pane is fully covered, so the user's focus is preserved.
    expect(
      isViewportStranded({
        pane: { width: 400, height: 300 },
        viewport: { x: -50, y: -600, zoom: 1.8 },
        bounds: chainBounds,
      }),
    ).toBe(false);
  });

  test("half the chain visible after a mild resize is not stranded", () => {
    expect(
      isViewportStranded({
        pane: { width: 500, height: 800 },
        viewport: { x: 100, y: 0, zoom: 1 },
        bounds: chainBounds,
      }),
    ).toBe(false);
  });

  test("empty flows and hidden panes never count as stranded", () => {
    expect(
      isViewportStranded({
        pane: { width: 500, height: 800 },
        viewport: { x: 0, y: 0, zoom: 1 },
        bounds: { x: 0, y: 0, width: 0, height: 0 },
      }),
    ).toBe(false);
    expect(
      isViewportStranded({
        pane: { width: 0, height: 0 },
        viewport: { x: 0, y: 0, zoom: 1 },
        bounds: chainBounds,
      }),
    ).toBe(false);
  });
});
