import { create } from "zustand";
import type {
  WorkflowBlock,
  WorkflowParameter,
} from "@/routes/workflows/types/workflowTypes";

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
  applicationNonce: number;
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
  applicationNonce: 0,
  setRecordedBlocks: ({ blocks, parameters }, insertionPoint) => {
    set({
      blocks,
      parameters,
      insertionPoint,
      applicationNonce: Date.now(),
    });
  },
  clearRecordedBlocks: () => {
    set({
      blocks: null,
      parameters: null,
      insertionPoint: null,
      applicationNonce: 0,
    });
  },
}));

export { useRecordedBlocksStore };
export type { InsertionPoint };
