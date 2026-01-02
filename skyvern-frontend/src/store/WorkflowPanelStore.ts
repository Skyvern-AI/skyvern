import { create } from "zustand";
import { WorkflowVersion } from "@/routes/workflows/hooks/useWorkflowVersionsQuery";

export type BranchContext = {
  conditionalNodeId: string;
  conditionalLabel: string;
  branchId: string;
  mergeLabel: string | null;
};

type WorkflowPanelState = {
  active: boolean;
  content:
    | "cacheKeyValues"
    | "parameters"
    | "nodeLibrary"
    | "history"
    | "comparison";
  data?: {
    previous?: string | null;
    next?: string | null;
    parent?: string;
    connectingEdgeType?: string;
    disableLoop?: boolean;
    branchContext?: BranchContext;
    // For comparison panel
    version1?: WorkflowVersion;
    version2?: WorkflowVersion;
    showComparison?: boolean;
  };
};

type WorkflowPanelStore = {
  workflowPanelState: WorkflowPanelState;
  closeWorkflowPanel: () => void;
  setWorkflowPanelState: (state: WorkflowPanelState) => void;
  toggleWorkflowPanel: () => void;
};

const useWorkflowPanelStore = create<WorkflowPanelStore>((set, get) => {
  return {
    workflowPanelState: {
      active: false,
      content: "parameters",
    },
    setWorkflowPanelState: (workflowPanelState: WorkflowPanelState) => {
      set({ workflowPanelState });
    },
    closeWorkflowPanel: () => {
      set({
        workflowPanelState: {
          ...get().workflowPanelState,
          active: false,
        },
      });
    },
    toggleWorkflowPanel: () => {
      set({
        workflowPanelState: {
          ...get().workflowPanelState,
          active: !get().workflowPanelState.active,
        },
      });
    },
  };
});

export { useWorkflowPanelStore };
