import { create } from "zustand";
import type { WorkflowBlock } from "@/routes/workflows/types/workflowTypes";

type InsertionPoint = {
  previous: string | null;
  next: string | null;
  parent?: string;
  connectingEdgeType: string;
};

type RecordedBlocksState = {
  blocks: Array<WorkflowBlock> | null;
  insertionPoint: InsertionPoint | null;
};

type RecordedBlocksStore = RecordedBlocksState & {
  setRecordedBlocks: (
    blocks: Array<WorkflowBlock>,
    insertionPoint: InsertionPoint,
  ) => void;
  clearRecordedBlocks: () => void;
};

const useRecordedBlocksStore = create<RecordedBlocksStore>((set) => ({
  blocks: null,
  insertionPoint: null,
  setRecordedBlocks: (blocks, insertionPoint) => {
    set({ blocks, insertionPoint });
  },
  clearRecordedBlocks: () => {
    set({ blocks: null, insertionPoint: null });
  },
}));

export { useRecordedBlocksStore };
export type { InsertionPoint };
