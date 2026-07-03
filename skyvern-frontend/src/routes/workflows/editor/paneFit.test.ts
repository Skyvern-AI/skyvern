import { describe, expect, test } from "vitest";

import {
  isMeaningfulPaneResize,
  isViewportStranded,
  PANE_RESIZE_EPSILON_PX,
  paneRecenterViewport,
  START_ANCHOR_MARGIN_X_PX,
  START_ANCHOR_MIN_ZOOM,
  START_ANCHOR_TOP_PX,
  startAnchoredViewport,
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

describe("startAnchoredViewport", () => {
  // A long chain: taller than any pane, so a whole-graph fit would zoom far
  // out and center the middle.
  const longChain = { x: 0, y: 0, width: 500, height: 4000 };

  test("anchors the flow start at the pane top at 1:1 zoom when the chain fits", () => {
    expect(
      startAnchoredViewport({
        pane: { width: 800, height: 600 },
        bounds: longChain,
      }),
    ).toEqual({ x: 150, y: START_ANCHOR_TOP_PX, zoom: 1 });
  });

  test("never zooms in past 1:1 on a very wide pane", () => {
    const viewport = startAnchoredViewport({
      pane: { width: 2000, height: 900 },
      bounds: longChain,
    });
    expect(viewport?.zoom).toBe(1);
    expect(viewport?.x).toBe((2000 - 500) / 2);
  });

  test("zooms out to fit the chain width in a narrow pane", () => {
    const pane = { width: 424, height: 600 };
    const viewport = startAnchoredViewport({ pane, bounds: longChain });
    expect(viewport?.zoom).toBeCloseTo(
      (pane.width - 2 * START_ANCHOR_MARGIN_X_PX) / longChain.width,
    );
    // Fit-width leaves exactly the margin as equal gutters.
    expect(viewport?.x).toBeCloseTo(START_ANCHOR_MARGIN_X_PX);
  });

  test("respects the canvas minimum zoom in an over-tight pane", () => {
    const viewport = startAnchoredViewport({
      pane: { width: 220, height: 600 },
      bounds: longChain,
    });
    expect(viewport?.zoom).toBe(START_ANCHOR_MIN_ZOOM);
    // Still centered: overflow crops both sides equally.
    expect(viewport?.x).toBeCloseTo((220 - 500 * START_ANCHOR_MIN_ZOOM) / 2);
  });

  test("offset bounds still land the first block at the top margin", () => {
    const bounds = { x: -100, y: 300, width: 500, height: 2000 };
    const viewport = startAnchoredViewport({
      pane: { width: 800, height: 600 },
      bounds,
    });
    expect(viewport).not.toBeNull();
    const { x, y, zoom } = viewport!;
    // Screen-space position of the flow's top-left corner.
    expect(bounds.y * zoom + y).toBeCloseTo(START_ANCHOR_TOP_PX);
    expect(bounds.x * zoom + x).toBeCloseTo((800 - 500) / 2);
  });

  test("degenerate panes and empty flows return null", () => {
    expect(
      startAnchoredViewport({
        pane: { width: 0, height: 0 },
        bounds: longChain,
      }),
    ).toBeNull();
    expect(
      startAnchoredViewport({
        pane: { width: 800, height: 600 },
        bounds: { x: 0, y: 0, width: 0, height: 0 },
      }),
    ).toBeNull();
  });
});

describe("paneRecenterViewport", () => {
  const chain = { x: 0, y: 0, width: 500, height: 4000 };

  test("recenters horizontally and keeps the scroll position at unchanged zoom", () => {
    // Fitted for a 1000-wide pane (x=250), scrolled into the flow (y=-800),
    // then a sibling pane opened and the pane shrank to 700.
    expect(
      paneRecenterViewport({
        pane: { width: 700, height: 600 },
        bounds: chain,
        viewport: { x: 250, y: -800, zoom: 1 },
      }),
    ).toEqual({ x: 100, y: -800, zoom: 1 });
  });

  test("scales the scroll anchor when the new width changes zoom", () => {
    const pane = { width: 424, height: 600 };
    const viewport = paneRecenterViewport({
      pane,
      bounds: chain,
      viewport: { x: 250, y: -800, zoom: 1 },
    });
    expect(viewport).not.toBeNull();
    const expectedZoom = (pane.width - 2 * START_ANCHOR_MARGIN_X_PX) / 500;
    expect(viewport!.zoom).toBeCloseTo(expectedZoom);
    // The content point at the pane's top edge stays put across the zoom.
    expect(-viewport!.y / viewport!.zoom).toBeCloseTo(800);
  });

  test("is a no-op for an already start-anchored viewport", () => {
    const pane = { width: 800, height: 600 };
    const anchored = startAnchoredViewport({ pane, bounds: chain });
    expect(anchored).not.toBeNull();
    expect(
      paneRecenterViewport({ pane, bounds: chain, viewport: anchored! }),
    ).toEqual(anchored);
  });

  test("degenerate geometry returns null", () => {
    expect(
      paneRecenterViewport({
        pane: { width: 0, height: 0 },
        bounds: chain,
        viewport: { x: 0, y: 0, zoom: 1 },
      }),
    ).toBeNull();
    expect(
      paneRecenterViewport({
        pane: { width: 800, height: 600 },
        bounds: chain,
        viewport: { x: 0, y: 0, zoom: 0 },
      }),
    ).toBeNull();
  });
});
