import { create } from "zustand";

export type WorkflowPresentation = "visual" | "doc";

type WorkflowPresentationStore = {
  presentation: WorkflowPresentation;
  workflowPermanentId: string | null;
  enabled: boolean;
  generation: number;
  syncScope: (workflowPermanentId: string | null, enabled: boolean) => void;
  setPresentation: (presentation: WorkflowPresentation) => void;
};

// YAML owns its transaction and active state. Presentation is its return target.
export const useWorkflowPresentationStore = create<WorkflowPresentationStore>(
  (set, get) => ({
    presentation: "visual",
    workflowPermanentId: null,
    enabled: false,
    generation: 0,
    syncScope: (workflowPermanentId, enabled) => {
      const state = get();
      if (
        state.workflowPermanentId === workflowPermanentId &&
        state.enabled === enabled
      )
        return;
      set({
        workflowPermanentId,
        enabled,
        presentation: "visual",
        generation: state.generation + 1,
      });
    },
    setPresentation: (presentation) => {
      set({
        presentation:
          presentation === "doc" && !get().enabled ? "visual" : presentation,
      });
    },
  }),
);
