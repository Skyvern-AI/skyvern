import { create } from "zustand";

type WorkflowPanelState = {
  active: boolean;
  content: "parameters" | "nodeLibrary";
  data?: {
    previous?: string | null;
    next?: string | null;
    parent?: string;
    connectingEdgeType?: string;
    disableLoop?: boolean;
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
