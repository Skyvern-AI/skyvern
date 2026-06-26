import type { NodeDimensionChange } from "@xyflow/react";

import type { AppNode } from "./nodes";

// React Flow's ResizeObserver reports sub-pixel measurements that drift between
// render cycles. Quantizing to whole pixels before comparison keeps that jitter
// from registering as a real dimension change and re-arming the layout pass.
export function quantizeDimension(
  value: number | undefined,
): number | undefined {
  return value === undefined ? undefined : Math.round(value);
}

// Upper bound on how many consecutive layout-triggering passes a single node may
// drive before its layouts are suppressed. A Dagre relayout normally converges
// in one pass; this only trips when one node's measurement and layout feed back
// into each other (the React error #185 "Maximum update depth exceeded" loop).
// Counting per node id keeps a burst of distinct nodes settling one after
// another (paste, recorded blocks, async content) from looking like a loop.
export const MAX_DIMENSION_LAYOUT_PASSES = 3;

// Re-arm a node's pass budget once it has been quiet for this long. React Flow
// emits a dimensions change only when measured size differs, so a node can never
// re-arm itself via a no-op "settled" event; without a time-based signal a node
// that legitimately resizes more than MAX_DIMENSION_LAYOUT_PASSES times (typing
// into an auto-growing block, toggling expandable content) would stay suppressed
// and leave the canvas laid out with stale positions. The debounced layout is
// hard-capped at maxWait=200ms, so a sustained #185 feedback loop re-emits a
// node's dimensions at most ~250ms apart and never opens a gap this wide; only a
// human-paced resize sequence does. Revisit this if that debounce changes.
export const DIMENSION_CONVERGENCE_QUIET_WINDOW_MS = 500;

export type DimensionConvergenceState = {
  passCounts: Map<string, number>;
  lastChangeAt: Map<string, number>;
};

export function createDimensionConvergenceState(): DimensionConvergenceState {
  return { passCounts: new Map(), lastChangeAt: new Map() };
}

// Re-arm every node's budget after a genuine user action (an edit, or focusing a
// different block) so intentional resizes are never starved by a prior loop.
export function resetDimensionConvergence(
  state: DimensionConvergenceState,
): void {
  state.passCounts.clear();
  state.lastChangeAt.clear();
}

type ProcessDimensionChangesOptions = {
  maxPasses?: number;
  quietWindowMs?: number;
  now?: number;
};

// Updates measured dimensions on the matching nodes IN PLACE so that successive
// change batches within the same render cycle don't re-count one settling
// resize; the returned array intentionally shares those node objects with the
// caller's input.
export function processDimensionChanges(
  nodes: Array<AppNode>,
  dimensionChanges: Array<NodeDimensionChange>,
  state: DimensionConvergenceState,
  options: ProcessDimensionChangesOptions = {},
): { nodes: Array<AppNode>; shouldLayout: boolean } {
  const {
    maxPasses = MAX_DIMENSION_LAYOUT_PASSES,
    quietWindowMs = DIMENSION_CONVERGENCE_QUIET_WINDOW_MS,
    now = Date.now(),
  } = options;

  const tempNodes = [...nodes];
  const changedNodeIds: Array<string> = [];

  for (const change of dimensionChanges) {
    const node = tempNodes.find((candidate) => candidate.id === change.id);
    if (!node) {
      continue;
    }

    const newWidth = quantizeDimension(change.dimensions?.width);
    const newHeight = quantizeDimension(change.dimensions?.height);

    if (
      quantizeDimension(node.measured?.width) !== newWidth ||
      quantizeDimension(node.measured?.height) !== newHeight
    ) {
      changedNodeIds.push(change.id);
      node.measured = {
        ...node.measured,
        width: newWidth,
        height: newHeight,
      };
    }
  }

  if (changedNodeIds.length === 0) {
    return { nodes: tempNodes, shouldLayout: false };
  }

  // Lay out as long as at least one changed node is still within its budget.
  // A feedback loop keeps re-changing the same node(s) and exhausts theirs; a
  // burst of distinct nodes each carries its own fresh budget.
  let withinBudget = false;
  for (const id of changedNodeIds) {
    // Re-arm a node whose last real change predates the quiet window: it can't
    // be part of an in-progress feedback loop, so a fresh resize starts over.
    // Anchored on every real change (including budget-exhausted ones) so a live
    // loop, which keeps firing inside the window, can never re-arm itself.
    const lastChangeAt = state.lastChangeAt.get(id);
    if (lastChangeAt !== undefined && now - lastChangeAt >= quietWindowMs) {
      state.passCounts.delete(id);
    }
    state.lastChangeAt.set(id, now);

    const count = (state.passCounts.get(id) ?? 0) + 1;
    state.passCounts.set(id, count);
    if (count <= maxPasses) {
      withinBudget = true;
    }
  }

  return { nodes: tempNodes, shouldLayout: withinBudget };
}
