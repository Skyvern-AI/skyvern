import { create } from "zustand";

import type { RunBlockingBlock } from "./getRunBlockingBlocks";

type RunValidationStore = {
  /** Blocks that must be fixed before the workflow can run. */
  blockingBlocks: Array<RunBlockingBlock>;
  blockingBlockIds: ReadonlySet<string>;
  setBlockingBlocks: (blocks: Array<RunBlockingBlock>) => void;
};

function blockKey(block: RunBlockingBlock): string {
  return `${block.id}:${block.label}`;
}

function sameBlocks(
  a: Array<RunBlockingBlock>,
  b: Array<RunBlockingBlock>,
): boolean {
  if (a.length !== b.length) {
    return false;
  }
  const aKeys = new Set(a.map(blockKey));
  return (
    aKeys.size === b.length && b.every((block) => aKeys.has(blockKey(block)))
  );
}

function blockIdSet(blocks: Array<RunBlockingBlock>): ReadonlySet<string> {
  return new Set(blocks.map((block) => block.id));
}

export const useRunValidationStore = create<RunValidationStore>((set, get) => ({
  blockingBlocks: [],
  blockingBlockIds: new Set(),
  setBlockingBlocks: (blocks) => {
    if (sameBlocks(get().blockingBlocks, blocks)) {
      return;
    }
    set({ blockingBlocks: blocks, blockingBlockIds: blockIdSet(blocks) });
  },
}));
