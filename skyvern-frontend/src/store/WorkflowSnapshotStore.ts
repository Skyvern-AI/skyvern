import { create } from "zustand";

import {
  isDraftDirty,
  snapshotOf,
  type WorkflowSnapshot,
} from "@/routes/workflows/editor/workflowChangesSummary";

import { useWorkflowHasChangesStore } from "./WorkflowHasChangesStore";

// The clean-baseline snapshot for the unsaved-changes indicator + summary.
// Frozen at the first user edit after a load/save (so post-load canvas
// materialization — e.g. login-block credential autofill — is absorbed into the
// baseline instead of reading as a user edit), and cleared when the workflow
// returns to a clean state. `contentDirty` drives the studio save-button dot.
// `userHasEdited` gates whether a later non-user change is absorbed or surfaced.
type WorkflowSnapshotStore = {
  snapshot: WorkflowSnapshot | null;
  contentDirty: boolean;
  userHasEdited: boolean;
  captureSnapshot: () => void;
  clearSnapshot: () => void;
  // Record a draft change. `userDriven` = the change followed a user gesture
  // (edit) vs. arrived on its own (post-load materialization).
  noteDraftChange: (userDriven: boolean) => void;
  // Mark the workflow user-edited for a change that has no canvas gesture but is
  // still user-driven — a Copilot build. Keeps the next draft change from being
  // absorbed as materialization; captures a baseline first if none exists yet.
  markUserEdit: () => void;
};

export const useWorkflowSnapshotStore = create<WorkflowSnapshotStore>(
  (set) => ({
    snapshot: null,
    contentDirty: false,
    userHasEdited: false,
    captureSnapshot: () => {
      const saveData = useWorkflowHasChangesStore.getState().getSaveData();
      set({
        snapshot: saveData ? snapshotOf(saveData) : null,
        contentDirty: false,
        userHasEdited: false,
      });
    },
    clearSnapshot: () =>
      set({ snapshot: null, contentDirty: false, userHasEdited: false }),
    noteDraftChange: (userDriven) =>
      set((state) => {
        const saveData = useWorkflowHasChangesStore.getState().getSaveData();
        if (!saveData || state.snapshot === null) {
          return { contentDirty: false };
        }
        const dirty = isDraftDirty(saveData, state.snapshot);
        // A change that diverges the draft before the first user edit and isn't
        // user-driven is post-load materialization (e.g. a login credential
        // autofill resolving after an early interaction). Absorb it into the
        // baseline instead of surfacing a phantom edit; the broad
        // autofill-on-mount case is tracked separately.
        if (!state.userHasEdited && !userDriven && dirty) {
          return { snapshot: snapshotOf(saveData), contentDirty: false };
        }
        // Only a user-driven change that actually diverges the draft counts as
        // the first edit — a bare selection/drag recomputes the graph without
        // changing content and must not freeze the baseline against a later
        // materialization.
        const userHasEdited = state.userHasEdited || (userDriven && dirty);
        return { userHasEdited, contentDirty: dirty };
      }),
    markUserEdit: () =>
      set((state) => {
        const saveData = useWorkflowHasChangesStore.getState().getSaveData();
        // Capture the pre-change state as the baseline if there isn't one yet
        // (e.g. a Copilot build before any manual edit), so the incoming change
        // diffs against it rather than being absorbed.
        const snapshot =
          state.snapshot ?? (saveData ? snapshotOf(saveData) : null);
        return { snapshot, userHasEdited: true };
      }),
  }),
);
