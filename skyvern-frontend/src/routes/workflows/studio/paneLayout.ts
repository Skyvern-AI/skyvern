import { arrayMove } from "@dnd-kit/sortable";

import { sanitizePaneWidth, type PaneWidths } from "@/store/paneWidths";

import { STUDIO_PANE_MIN_WIDTH, type StudioPaneId } from "./panes";

// Re-exported for the shell: pinned widths ride along with the layout math.
export type { PaneWidths };

// Non-greedy panes sit at this width by default; the greedy pane absorbs the
// rest of the row.
export const STUDIO_PANE_DEFAULT_WIDTH = 300;

// The browser is the best consumer of free space; the canvas takes over when
// the browser is closed. With neither open, every open pane flexes equally.
export function greedyPaneOf(
  panes: readonly StudioPaneId[],
): StudioPaneId | undefined {
  if (panes.includes("browser")) {
    return "browser";
  }
  if (panes.includes("editor")) {
    return "editor";
  }
  return undefined;
}

// A pane can hold a pinned px width unless it is the row's flexing pane: the
// greedy pane, or the last pane when no greedy pane is open. Exactly one open
// pane always flexes, so the row fills with no voids and no horizontal scroll.
export function paneResizable(
  id: StudioPaneId,
  panes: readonly StudioPaneId[],
): boolean {
  const greedy = greedyPaneOf(panes);
  if (greedy !== undefined) {
    return id !== greedy;
  }
  return panes.indexOf(id) !== panes.length - 1;
}

// CSS flex shorthand for an open pane. Pinned panes keep flex-shrink so a
// narrow window squeezes them toward their min-width instead of overflowing.
export function paneFlex(
  id: StudioPaneId,
  panes: readonly StudioPaneId[],
  widths: PaneWidths,
): string {
  const greedy = greedyPaneOf(panes);
  if (id === greedy) {
    return "1 1 0%";
  }
  if (!paneResizable(id, panes)) {
    // Last pane of a greedy-less row: flexes so the row always fills.
    return `1 1 ${STUDIO_PANE_DEFAULT_WIDTH}px`;
  }
  const pinned = sanitizePaneWidth(widths[id]);
  if (pinned !== undefined) {
    return `0 1 ${pinned}px`;
  }
  // Without a greedy pane, unpinned panes share the free space equally.
  return greedy === undefined
    ? `1 1 ${STUDIO_PANE_DEFAULT_WIDTH}px`
    : `0 1 ${STUDIO_PANE_DEFAULT_WIDTH}px`;
}

// Clamp a divider drag so neither neighbor goes under its min width. The
// neighbors' total is preserved, so panes elsewhere in the row never move.
// Stable serialization of the committed divider widths, for change detection
// (the editor re-fits on width commits via FlowRenderer's paneLayoutKey).
export function paneWidthsKey(widths: PaneWidths): string {
  return Object.entries(widths)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([id, width]) => `${id}:${width}`)
    .join(",");
}

export function clampResizeDelta(
  delta: number,
  left: { id: StudioPaneId; width: number },
  right: { id: StudioPaneId; width: number },
): number {
  return Math.min(
    Math.max(delta, STUDIO_PANE_MIN_WIDTH[left.id] - left.width),
    right.width - STUDIO_PANE_MIN_WIDTH[right.id],
  );
}

// arrayMove semantics, shared with the editor's block drag: the dragged pane
// takes the target pane's slot.
export function movePaneTo(
  panes: readonly StudioPaneId[],
  activeId: StudioPaneId,
  overId: StudioPaneId,
): StudioPaneId[] {
  const from = panes.indexOf(activeId);
  const to = panes.indexOf(overId);
  if (from < 0 || to < 0 || from === to) {
    return [...panes];
  }
  return arrayMove([...panes], from, to);
}

export function movePaneBy(
  panes: readonly StudioPaneId[],
  id: StudioPaneId,
  direction: -1 | 1,
): StudioPaneId[] {
  const from = panes.indexOf(id);
  const to = from + direction;
  if (from < 0 || to < 0 || to >= panes.length) {
    return [...panes];
  }
  return arrayMove([...panes], from, to);
}
