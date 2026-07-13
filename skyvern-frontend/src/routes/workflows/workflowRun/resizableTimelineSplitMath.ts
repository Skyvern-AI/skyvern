export const TIMELINE_SPLIT_STORAGE_KEY = "skyvern.runView.timelineSplit";
export const DEFAULT_SPLIT_FRACTION = 0.5;
export const DIVIDER_HEIGHT_PX = 12;
// Measured (real-layout headless Chrome, getBoundingClientRect) against the
// two panes' actual always-present header chrome: WorkflowRunTimeline's own
// header row is ~33px; BlockDetailHeader's (blockDetail/shared.tsx) compound
// info+meta rows are ~61px, the taller of the two. 120px clears both with
// room for a few lines of body content and never clips a header — if either
// header grows, re-measure and bump this constant to match.
export const MIN_PANE_HEIGHT_PX = 120;
export const DIVIDER_KEY_STEP_PX = 24;
// CSS grid (like flexbox) special-cases flex factors that sum to < 1: it only
// distributes that fraction of the leftover space, leaving a sliver
// unfilled. A float round-tripped through localStorage can land the two
// factors just under 1 (e.g. 0.4999999999999999 + 0.5). Emitting integer fr
// values that always sum to ROW_SCALE (far above 1) avoids that range
// entirely.
const ROW_SCALE = 1000;

export function sanitizeSplitFraction(value: unknown): number {
  return typeof value === "number" &&
    Number.isFinite(value) &&
    value > 0 &&
    value < 1
    ? value
    : DEFAULT_SPLIT_FRACTION;
}

export function readStoredFraction(): number {
  try {
    const raw = localStorage.getItem(TIMELINE_SPLIT_STORAGE_KEY);
    return raw === null
      ? DEFAULT_SPLIT_FRACTION
      : sanitizeSplitFraction(JSON.parse(raw));
  } catch {
    // Safari private-mode throws on access, not just on write.
    return DEFAULT_SPLIT_FRACTION;
  }
}

export function persistFraction(fraction: number) {
  try {
    localStorage.setItem(TIMELINE_SPLIT_STORAGE_KEY, JSON.stringify(fraction));
  } catch {
    // Quota exceeded or storage blocked — losing persistence is fine.
  }
}

// Clamp a divider drag so neither pane goes under its min height.
// contentHeightPx excludes the divider's own fixed height. A container too
// short to honor both floors falls back to an even split (grid's
// minmax(0, Nfr) rows still degrade proportionally with the space actually
// available — they never overflow the container).
export function clampSplitFraction(
  topPx: number,
  contentHeightPx: number,
): number {
  if (contentHeightPx <= MIN_PANE_HEIGHT_PX * 2) {
    return DEFAULT_SPLIT_FRACTION;
  }
  const clampedTopPx = Math.min(
    Math.max(topPx, MIN_PANE_HEIGHT_PX),
    contentHeightPx - MIN_PANE_HEIGHT_PX,
  );
  return clampedTopPx / contentHeightPx;
}

export function gridTemplateRowsFor(fraction: number): string {
  const top = Math.round(fraction * ROW_SCALE);
  const bottom = ROW_SCALE - top;
  return `minmax(0, ${top}fr) ${DIVIDER_HEIGHT_PX}px minmax(0, ${bottom}fr)`;
}
