import type { SortableScopeNode } from "./scope";

// Shared predicates for the finally-block gate. `finallyBlockLabel` only
// applies at the workflow root; nested scopes (loops / branches) keep
// their own reorder semantics.

export function isBlockFinallyGated(
  blockLabel: string,
  finallyBlockLabel: string | null,
): boolean {
  if (!finallyBlockLabel) return false;
  return finallyBlockLabel === blockLabel;
}

export type FinallyBlockCandidateNode = SortableScopeNode & {
  data?: { label?: string };
};

// Label-uniqueness invariant: top-level block labels are unique within a
// workflow definition (enforced upstream by validateUniqueLabels at save
// time, and assumed by Jinja `{{label.field}}` refs and conditional
// next_block_label routing). First-match here is therefore safe.
export function findFinallyBlockNodeId(
  nodes: Array<FinallyBlockCandidateNode>,
  finallyBlockLabel: string | null,
): string | null {
  if (!finallyBlockLabel) return null;
  for (const node of nodes) {
    if (node.parentId) continue;
    // start / nodeAdder are anchor nodes; matching them would be a
    // malformed setting, so the gate stays on real top-level blocks.
    if (node.type === "start" || node.type === "nodeAdder") continue;
    const label = node.data?.label;
    if (label && label === finallyBlockLabel) return node.id;
  }
  return null;
}
