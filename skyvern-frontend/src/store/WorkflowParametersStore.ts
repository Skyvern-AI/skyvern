import { create } from "zustand";
import { ParametersState } from "@/routes/workflows/editor/types";

interface WorkflowParametersStore {
  parameters: ParametersState;
  setParameters: (parameters: ParametersState) => void;
}

const useWorkflowParametersStore = create<WorkflowParametersStore>((set) => {
  return {
    parameters: [],
    setParameters: (parameters: ParametersState) => set({ parameters }),
  };
});

export { useWorkflowParametersStore };
