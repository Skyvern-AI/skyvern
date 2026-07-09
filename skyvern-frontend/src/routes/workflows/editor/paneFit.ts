// Pure helpers for the studio editor pane's fit-view policy. The pane resizes
// in discrete steps (sibling panes toggling, window resizes), so the canvas
// re-fits only when a real geometry change leaves the viewport stranded.

type Size = { width: number; height: number };
type Viewport = { x: number; y: number; zoom: number };
type Rect = { x: number; y: number; width: number; height: number };

// Settle after a resize burst finishes; reacting per ResizeObserver frame
// re-arms the canvas's dimension→layout cycle faster than its re-entrancy
// budget resets, which drops the final settled relayout.
export const PANE_FIT_DEBOUNCE_MS = 200;

// Sub-pixel flex settling and scrollbar appearance jitter below this delta.
export const PANE_RESIZE_EPSILON_PX = 8;

// Below this share, the visible slice of the flow reads as "lost canvas".
// 0.3 keeps a half-visible chain (mild resize) untouched while a wide-fit
// viewport squeezed into a ~330px pane (~10-25% left visible) re-fits.
const STRANDED_VISIBLE_FRACTION = 0.3;

export function isMeaningfulPaneResize(prev: Size, next: Size): boolean {
  return (
    Math.abs(prev.width - next.width) >= PANE_RESIZE_EPSILON_PX ||
    Math.abs(prev.height - next.height) >= PANE_RESIZE_EPSILON_PX
  );
}

/**
 * A viewport is stranded when the intersection of the flow's bounding box
 * (screen space) with the pane is a sliver: it covers little of the pane AND
 * shows little of the flow. A user deliberately zoomed into one block keeps
 * their viewport (the block still fills the pane); a pane that shrank past the
 * chain gets re-fit.
 */
export function isViewportStranded({
  pane,
  viewport,
  bounds,
}: {
  pane: Size;
  viewport: Viewport;
  bounds: Rect;
}): boolean {
  if (pane.width <= 0 || pane.height <= 0) {
    return false;
  }
  if (bounds.width <= 0 || bounds.height <= 0) {
    return false;
  }
  const screenLeft = bounds.x * viewport.zoom + viewport.x;
  const screenTop = bounds.y * viewport.zoom + viewport.y;
  const screenWidth = bounds.width * viewport.zoom;
  const screenHeight = bounds.height * viewport.zoom;

  const intersectWidth = Math.max(
    0,
    Math.min(screenLeft + screenWidth, pane.width) - Math.max(screenLeft, 0),
  );
  const intersectHeight = Math.max(
    0,
    Math.min(screenTop + screenHeight, pane.height) - Math.max(screenTop, 0),
  );
  const intersectArea = intersectWidth * intersectHeight;

  const flowVisibleFraction = intersectArea / (screenWidth * screenHeight);
  const paneCoveredFraction = intersectArea / (pane.width * pane.height);
  return (
    flowVisibleFraction < STRANDED_VISIBLE_FRACTION &&
    paneCoveredFraction < STRANDED_VISIBLE_FRACTION
  );
}

export function paneRefitDuration(): number {
  if (
    typeof window === "undefined" ||
    typeof window.matchMedia !== "function"
  ) {
    return 0;
  }
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ? 0
    : 150;
}

// The legacy editor renders blocks at 1:1; the start anchor only zooms out
// when the chain can't fit the pane width. The floor is also the canvas's
// minZoom prop, so the computed x-centering matches the zoom actually applied.
export const START_ANCHOR_MAX_ZOOM = 1;
export const START_ANCHOR_MIN_ZOOM = 0.5;

export const START_ANCHOR_MARGIN_X_PX = 24;
export const START_ANCHOR_TOP_PX = 24;

/**
 * Viewport that anchors the flow's START at the pane top, horizontally
 * centered, at a legacy-like zoom (1:1, or fit-width when the pane is
 * narrower than the chain). Unlike a whole-graph fitView, a long flow loads
 * reading from its first block instead of centered on its middle.
 */
export function startAnchoredViewport({
  pane,
  bounds,
}: {
  pane: Size;
  bounds: Rect;
}): Viewport | null {
  if (pane.width <= 0 || pane.height <= 0) {
    return null;
  }
  if (bounds.width <= 0 || bounds.height <= 0) {
    return null;
  }
  const fitWidthZoom =
    (pane.width - 2 * START_ANCHOR_MARGIN_X_PX) / bounds.width;
  const zoom = Math.min(
    START_ANCHOR_MAX_ZOOM,
    Math.max(START_ANCHOR_MIN_ZOOM, fitWidthZoom),
  );
  return {
    x: (pane.width - bounds.width * zoom) / 2 - bounds.x * zoom,
    y: START_ANCHOR_TOP_PX - bounds.y * zoom,
    zoom,
  };
}

export const END_ANCHOR_BOTTOM_PX = 24;

/**
 * Mirror of `startAnchoredViewport` for the flow's END: same zoom rule and
 * horizontal centering, with the flow's bottom edge anchored just above the
 * pane bottom. Jumping between the two anchors keeps the zoom stable.
 */
export function endAnchoredViewport({
  pane,
  bounds,
}: {
  pane: Size;
  bounds: Rect;
}): Viewport | null {
  const start = startAnchoredViewport({ pane, bounds });
  if (start === null) {
    return null;
  }
  return {
    x: start.x,
    y:
      pane.height -
      END_ANCHOR_BOTTOM_PX -
      (bounds.y + bounds.height) * start.zoom,
    zoom: start.zoom,
  };
}

/**
 * Which flow-jump buttons should show for the current viewport. Both hide
 * when the whole flow fits the pane at the current zoom (no dead chrome);
 * otherwise each shows only while its end of the flow is scrolled out of
 * view.
 */
export function flowJumpVisibility({
  pane,
  viewport,
  bounds,
}: {
  pane: Size;
  viewport: Viewport;
  bounds: Rect;
}): { showJumpToStart: boolean; showJumpToEnd: boolean } {
  if (
    pane.width <= 0 ||
    pane.height <= 0 ||
    bounds.width <= 0 ||
    bounds.height <= 0
  ) {
    return { showJumpToStart: false, showJumpToEnd: false };
  }
  const screenTop = bounds.y * viewport.zoom + viewport.y;
  const screenHeight = bounds.height * viewport.zoom;
  const flowTallerThanPane = screenHeight > pane.height;
  return {
    showJumpToStart: flowTallerThanPane && screenTop < 0,
    showJumpToEnd: flowTallerThanPane && screenTop + screenHeight > pane.height,
  };
}

/**
 * Viewport for a pane-layout change (panes opened/closed/reordered):
 * re-fits horizontally at the start-anchor zoom while keeping the content
 * point at the pane's top edge fixed, so the user's scroll position through
 * the flow survives the new geometry. Idempotent for an already-anchored
 * viewport, so back-to-back layout events converge.
 */
export function paneRecenterViewport({
  pane,
  bounds,
  viewport,
}: {
  pane: Size;
  bounds: Rect;
  viewport: Viewport;
}): Viewport | null {
  if (viewport.zoom <= 0) {
    return null;
  }
  const target = startAnchoredViewport({ pane, bounds });
  if (target === null) {
    return null;
  }
  return {
    x: target.x,
    y: viewport.y * (target.zoom / viewport.zoom),
    zoom: target.zoom,
  };
}
