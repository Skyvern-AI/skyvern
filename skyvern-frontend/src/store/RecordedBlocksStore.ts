import { create } from "zustand";
import type {
  WorkflowBlock,
  WorkflowParameter,
} from "@/routes/workflows/types/workflowTypes";

/**
 * A parameter synthesized from a recording: either a plain workflow parameter or a
 * credential parameter bound to a vault entry the user attached while recording.
 */
type RecordedParameter =
  | WorkflowParameter
  | {
      key: string;
      description?: string | null;
      parameter_type: "credential";
      credential_id: string;
    };

type InsertionPoint = {
  previous: string | null;
  next: string | null;
  parent?: string;
  connectingEdgeType: string;
};

type RecordedBlocksState = {
  blocks: Array<WorkflowBlock> | null;
  parameters: Array<RecordedParameter> | null;
  insertionPoint: InsertionPoint | null;
  applicationNonce: number;
};

type RecordedBlocksStore = RecordedBlocksState & {
  setRecordedBlocks: (
    data: {
      blocks: Array<WorkflowBlock>;
      parameters: Array<RecordedParameter>;
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
export type { InsertionPoint, RecordedParameter };
