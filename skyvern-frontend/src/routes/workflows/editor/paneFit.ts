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
