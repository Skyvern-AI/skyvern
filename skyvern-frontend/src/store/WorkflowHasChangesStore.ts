import { create } from "zustand";

type WorkflowHasChangesStore = {
  hasChanges: boolean;
  setHasChanges: (hasChanges: boolean) => void;
};

const useWorkflowHasChangesStore = create<WorkflowHasChangesStore>((set) => ({
  hasChanges: false,
  setHasChanges: (hasChanges) => set({ hasChanges }),
}));

export { useWorkflowHasChangesStore };
