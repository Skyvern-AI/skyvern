import { create } from "zustand";

import type { RunBlockingBlock } from "./getRunBlockingBlocks";
import type { RunBlockingPathSegment } from "./resolveBlockPath";

type RunValidationStore = {
  /** Blocks that must be fixed before the workflow can run. */
  blockingBlocks: Array<RunBlockingBlock>;
  blockingBlockIds: ReadonlySet<string>;
  /** Container node id -> number of blocking blocks nested inside it. */
  blockingDescendantCountById: ReadonlyMap<string, number>;
  setBlockingBlocks: (blocks: Array<RunBlockingBlock>) => void;
};

function pathKey(path: Array<RunBlockingPathSegment>): string {
  return path
    .map(
      (segment) => `${segment.kind}|${segment.label}|${segment.branch ?? ""}`,
    )
    .join(">");
}

// Keys the rendered row identity: id + label + ancestor ids + the breadcrumb
// (labels/branch) so renaming an ancestor or moving a block between branches of
// the same conditional refreshes the panel path, not just the count.
function blockKey(block: RunBlockingBlock): string {
  const location = block.ancestorIds.join(",");
  return `${block.id}:${block.label}:${location}:${pathKey(block.path)}`;
}

// Order-sensitive: reordering blocks must re-render the panel in the new order,
// so compare position by position rather than as sets.
function sameBlocks(
  a: Array<RunBlockingBlock>,
  b: Array<RunBlockingBlock>,
): boolean {
  if (a.length !== b.length) {
    return false;
  }
  const bKeys = b.map(blockKey);
  return a.every((block, index) => blockKey(block) === bKeys[index]);
}

function blockIdSet(blocks: Array<RunBlockingBlock>): ReadonlySet<string> {
  return new Set(blocks.map((block) => block.id));
}

function descendantCounts(
  blocks: Array<RunBlockingBlock>,
): ReadonlyMap<string, number> {
  const counts = new Map<string, number>();
  for (const block of blocks) {
    for (const ancestorId of block.ancestorIds) {
      counts.set(ancestorId, (counts.get(ancestorId) ?? 0) + 1);
    }
  }
  return counts;
}

export const useRunValidationStore = create<RunValidationStore>((set, get) => ({
  blockingBlocks: [],
  blockingBlockIds: new Set(),
  blockingDescendantCountById: new Map(),
  setBlockingBlocks: (blocks) => {
    if (sameBlocks(get().blockingBlocks, blocks)) {
      return;
    }
    set({
      blockingBlocks: blocks,
      blockingBlockIds: blockIdSet(blocks),
      blockingDescendantCountById: descendantCounts(blocks),
    });
  },
}));
