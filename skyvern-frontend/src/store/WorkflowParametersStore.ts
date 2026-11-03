import { useWorkflowTitleStore } from "./WorkflowTitleStore";
import { create } from "zustand";
import { ParametersState } from "@/routes/workflows/editor/types";
import {
  refuseMutationDuringYamlCommit,
  canInitializeWorkflow,
  reconcileYamlDraftAfterGraphChange,
  useWorkflowYamlEditorStore,
} from "./WorkflowYamlEditorStore";

interface WorkflowParametersStore {
  parameters: ParametersState;
  parametersWorkflowPermanentId: string | null;
  resetParametersSession: (workflowPermanentId: string) => void;
  setParametersFromUser: (parameters: ParametersState) => void;
  setParameters: (
    parameters: ParametersState,
    options?: { fromYamlCommit?: boolean; workflowPermanentId?: string },
  ) => boolean;
}

const useWorkflowParametersStore = create<WorkflowParametersStore>(
  (set, get) => {
    return {
      parameters: [],
      parametersWorkflowPermanentId: null,
      resetParametersSession: (workflowPermanentId) => {
        if (get().parametersWorkflowPermanentId === workflowPermanentId)
          set({ parametersWorkflowPermanentId: null });
      },
      setParametersFromUser: (parameters) => {
        if (refuseMutationDuringYamlCommit()) return;
        useWorkflowTitleStore.getState().recordCopilotGraphEdit();
        get().setParameters(parameters);
      },
      setParameters: (parameters: ParametersState, options) => {
        if (
          !options?.fromYamlCommit &&
          (options?.workflowPermanentId
            ? !canInitializeWorkflow(options.workflowPermanentId)
            : refuseMutationDuringYamlCommit())
        )
          return false;
        if (!options?.fromYamlCommit) reconcileYamlDraftAfterGraphChange();
        useWorkflowYamlEditorStore.getState().bumpRevision();
        set({
          parameters,
          ...(options?.workflowPermanentId
            ? {
                parametersWorkflowPermanentId: options.workflowPermanentId,
              }
            : {}),
        });
        return true;
      },
    };
  },
);

export { useWorkflowParametersStore };
