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

const storageKey = (workflowPermanentId: string) =>
  `skyvern.workflowPresentation.${workflowPermanentId}`;

function readStored(workflowPermanentId: string | null): WorkflowPresentation {
  if (!workflowPermanentId) return "visual";
  try {
    return localStorage.getItem(storageKey(workflowPermanentId)) === "doc"
      ? "doc"
      : "visual";
  } catch {
    return "visual";
  }
}

// An agent born from a demonstration opens in the Doc the first time; any later choice replaces this.
export function openNewAgentInDoc(workflowPermanentId: string) {
  writeStored(workflowPermanentId, "doc");
}

function writeStored(
  workflowPermanentId: string | null,
  presentation: WorkflowPresentation,
) {
  if (!workflowPermanentId) return;
  try {
    localStorage.setItem(storageKey(workflowPermanentId), presentation);
  } catch {
    // A choice that can't be remembered still applies to this tab.
  }
}

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
        // Only an agent whose Doc is available reopens in the Doc; with the flag off it is always Visual.
        presentation: enabled ? readStored(workflowPermanentId) : "visual",
        generation: state.generation + 1,
      });
    },
    setPresentation: (presentation) => {
      const { enabled, workflowPermanentId } = get();
      const next = presentation === "doc" && !enabled ? "visual" : presentation;
      set({ presentation: next });
      if (enabled) writeStored(workflowPermanentId, next);
    },
  }),
);
