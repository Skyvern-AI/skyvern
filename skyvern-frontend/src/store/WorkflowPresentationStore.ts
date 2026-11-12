import { create } from "zustand";

export type WorkflowPresentation = "visual" | "doc";

type WorkflowPresentationStore = {
  presentation: WorkflowPresentation;
  workflowPermanentId: string | null;
  enabled: boolean;
  generation: number;
  docMounted: boolean;
  setDocMounted: (mounted: boolean) => void;
  syncScope: (workflowPermanentId: string | null, enabled: boolean) => void;
  setPresentation: (presentation: WorkflowPresentation) => void;
  // True when it switched the Doc to Visual.
  showVisualUnremembered: () => boolean;
  restoreView: () => void;
};

const storageKey = (workflowPermanentId: string) =>
  `skyvern.workflowPresentation.${workflowPermanentId}`;

// With nothing stored (or storage unreadable) an agent opens in the Doc.
function readStored(workflowPermanentId: string | null): WorkflowPresentation {
  if (!workflowPermanentId) return "visual";
  try {
    return localStorage.getItem(storageKey(workflowPermanentId)) === "visual"
      ? "visual"
      : "doc";
  } catch {
    return "doc";
  }
}

// An agent born from a demonstration is pinned to the Doc; any later choice replaces this.
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
    docMounted: false,
    setDocMounted: (docMounted) => set({ docMounted }),
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
        // With the Doc unavailable (flag off, region, kill switch) every agent is Visual.
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
    // Only a person's click on a view is remembered; this switch lasts until restoreView.
    showVisualUnremembered: () => {
      const switched = get().presentation === "doc";
      if (switched) set({ presentation: "visual" });
      return switched;
    },
    // The person's remembered view, or the default, again.
    restoreView: () => {
      const { enabled, workflowPermanentId } = get();
      set({
        presentation: enabled ? readStored(workflowPermanentId) : "visual",
      });
    },
  }),
);
