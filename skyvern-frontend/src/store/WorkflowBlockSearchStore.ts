import { create } from "zustand";

import { type BlockSearchTarget } from "@/routes/workflows/studio/blockSearch";

type WorkflowBlockSearchHandle = {
  // Searchable blocks in canvas order, read fresh on each popover open.
  getTargets: () => Array<BlockSearchTarget>;
  // Selects the block and centers the canvas on it.
  focusBlock: (nodeId: string) => void;
};

type WorkflowBlockSearchState = {
  // Registered by the studio's embedded FlowRenderer (which owns the React
  // Flow instance) so the Editor pane header's search can jump the canvas
  // without owning it. Null while no searchable canvas is mounted — the
  // header hides the control.
  handle: WorkflowBlockSearchHandle | null;
  registerHandle: (handle: WorkflowBlockSearchHandle | null) => void;
};

export const useWorkflowBlockSearchStore = create<WorkflowBlockSearchState>(
  (set) => ({
    handle: null,
    registerHandle: (handle) => set({ handle }),
  }),
);
