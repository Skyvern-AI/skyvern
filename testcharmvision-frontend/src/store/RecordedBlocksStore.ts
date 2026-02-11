import { create } from "zustand";
import type { WorkflowBlock } from "@/routes/workflows/types/workflowTypes";
import type { WorkflowParameter } from "@/routes/workflows/types/workflowTypes";

type InsertionPoint = {
  previous: string | null;
  next: string | null;
  parent?: string;
  connectingEdgeType: string;
};

type RecordedBlocksState = {
  blocks: Array<WorkflowBlock> | null;
  parameters: Array<WorkflowParameter> | null;
  insertionPoint: InsertionPoint | null;
};

type RecordedBlocksStore = RecordedBlocksState & {
  setRecordedBlocks: (
    data: {
      blocks: Array<WorkflowBlock>;
      parameters: Array<WorkflowParameter>;
    },
    insertionPoint: InsertionPoint,
  ) => void;
  clearRecordedBlocks: () => void;
};

const useRecordedBlocksStore = create<RecordedBlocksStore>((set) => ({
  blocks: null,
  parameters: null,
  insertionPoint: null,
  setRecordedBlocks: ({ blocks, parameters }, insertionPoint) => {
    set({ blocks, parameters, insertionPoint });
  },
  clearRecordedBlocks: () => {
    set({ blocks: null, insertionPoint: null });
  },
}));

export { useRecordedBlocksStore };
export type { InsertionPoint };
