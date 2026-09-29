import { create } from "zustand";

export type CopilotBlockBuildRequest = {
  blockLabel: string;
  prompt: string;
  // The person changed this block's Goal and is asking for the block to follow it.
  applyingGoalChange?: boolean;
};

export type PendingGoalChange = {
  label: string;
  goal: string;
  // Null when the editor never saw the Goal before the change, so it cannot be undone here.
  previousGoal: string | null;
};

interface CopilotActionStore {
  // A pending request for the copilot to (re)build a single code block from its prompt.
  pendingBuild: CopilotBlockBuildRequest | null;
  // Label of the block currently generating, so the block can show a local busy state.
  generatingBlockLabel: string | null;
  // Requests made while another block was generating; each starts when the one before it finishes.
  queuedBuilds: Array<CopilotBlockBuildRequest>;
  // Bumped when the user stops an in-flight block generation.
  cancelNonce: number;
  // Blocks whose Goal a person changed and that have not been updated to match it yet. Published
  // from inside the canvas so surfaces outside it (the chat, the top bar) can read it.
  pendingGoalChanges: Array<PendingGoalChange>;
  undoGoalChange: (label: string) => void;
  setPendingGoalChanges: (changes: Array<PendingGoalChange>) => void;
  setUndoGoalChange: (undo: (label: string) => void) => void;
  requestBuild: (request: CopilotBlockBuildRequest) => void;
  applyPendingGoalChanges: () => void;
  clearPendingBuild: () => void;
  finishGenerating: () => void;
  requestCancel: () => void;
}

const noUndo = () => {};

export const useCopilotActionStore = create<CopilotActionStore>((set, get) => ({
  pendingBuild: null,
  generatingBlockLabel: null,
  queuedBuilds: [],
  cancelNonce: 0,
  pendingGoalChanges: [],
  undoGoalChange: noUndo,
  setPendingGoalChanges: (changes) =>
    set((state) =>
      JSON.stringify(state.pendingGoalChanges) === JSON.stringify(changes)
        ? state
        : { pendingGoalChanges: changes },
    ),
  setUndoGoalChange: (undo) => set({ undoGoalChange: undo }),
  requestBuild: (request) =>
    set((state) => {
      if (state.generatingBlockLabel == null) {
        return {
          pendingBuild: request,
          generatingBlockLabel: request.blockLabel,
        };
      }
      const alreadyWaiting =
        state.generatingBlockLabel === request.blockLabel ||
        state.queuedBuilds.some(
          (queued) => queued.blockLabel === request.blockLabel,
        );
      return alreadyWaiting
        ? state
        : { queuedBuilds: [...state.queuedBuilds, request] };
    }),
  applyPendingGoalChanges: () => {
    for (const change of get().pendingGoalChanges) {
      get().requestBuild({
        blockLabel: change.label,
        prompt: change.goal,
        applyingGoalChange: true,
      });
    }
  },
  clearPendingBuild: () => set({ pendingBuild: null }),
  finishGenerating: () =>
    set((state) => {
      const [next, ...rest] = state.queuedBuilds;
      return next
        ? {
            pendingBuild: next,
            generatingBlockLabel: next.blockLabel,
            queuedBuilds: rest,
          }
        : { generatingBlockLabel: null };
    }),
  requestCancel: () =>
    set((state) => ({
      pendingBuild: null,
      generatingBlockLabel: null,
      queuedBuilds: [],
      cancelNonce: state.cancelNonce + 1,
    })),
}));
