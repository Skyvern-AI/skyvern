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
  codeEditedByHand?: boolean;
};

export type GoalSuggestion = {
  forCode: string;
  forGoal: string;
  goal: string;
};

export type CodeEditedBlock = {
  label: string;
  goal: string;
  // Null until a suggestion written for the block's current code and Goal is in hand.
  suggestedGoal: string | null;
};

type CodeEditedGoalActions = {
  updateGoal: (label: string) => void;
  keepGoal: (label: string) => void;
  acceptGoal: (label: string) => void;
  keepCode: (label: string) => void;
};

interface CopilotActionStore extends CodeEditedGoalActions {
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
  // Blocks whose code a person edited by hand since their Goal was last confirmed.
  codeEditedBlocks: Array<CodeEditedBlock>;
  setCodeEditedBlocks: (blocks: Array<CodeEditedBlock>) => void;
  // Code blocks whose Goal the canvas refuses to change: not editable, or a read-only scope.
  readOnlyGoalLabels: Array<string>;
  setReadOnlyGoalLabels: (labels: Array<string>) => void;
  goalSuggestions: Record<string, GoalSuggestion>;
  setGoalSuggestion: (label: string, suggestion: GoalSuggestion | null) => void;
  suggestingGoalLabels: Array<string>;
  setSuggestingGoal: (label: string, suggesting: boolean) => void;
  setCodeEditedGoalActions: (actions: CodeEditedGoalActions) => void;
  requestBuild: (request: CopilotBlockBuildRequest) => void;
  applyPendingGoalChanges: () => void;
  clearPendingBuild: () => void;
  finishGenerating: () => void;
  requestCancel: () => void;
}

const noop = () => {};

export const useCopilotActionStore = create<CopilotActionStore>((set, get) => ({
  pendingBuild: null,
  generatingBlockLabel: null,
  queuedBuilds: [],
  cancelNonce: 0,
  pendingGoalChanges: [],
  undoGoalChange: noop,
  setPendingGoalChanges: (changes) =>
    set((state) =>
      JSON.stringify(state.pendingGoalChanges) === JSON.stringify(changes)
        ? state
        : { pendingGoalChanges: changes },
    ),
  setUndoGoalChange: (undo) => set({ undoGoalChange: undo }),
  codeEditedBlocks: [],
  setCodeEditedBlocks: (blocks) =>
    set((state) =>
      JSON.stringify(state.codeEditedBlocks) === JSON.stringify(blocks)
        ? state
        : { codeEditedBlocks: blocks },
    ),
  readOnlyGoalLabels: [],
  setReadOnlyGoalLabels: (labels) =>
    set((state) =>
      JSON.stringify(state.readOnlyGoalLabels) === JSON.stringify(labels)
        ? state
        : { readOnlyGoalLabels: labels },
    ),
  goalSuggestions: {},
  setGoalSuggestion: (label, suggestion) =>
    set((state) => {
      const goalSuggestions = { ...state.goalSuggestions };
      delete goalSuggestions[label];
      if (suggestion) {
        goalSuggestions[label] = suggestion;
      }
      return { goalSuggestions };
    }),
  suggestingGoalLabels: [],
  setSuggestingGoal: (label, suggesting) =>
    set((state) => {
      const rest = state.suggestingGoalLabels.filter(
        (candidate) => candidate !== label,
      );
      return { suggestingGoalLabels: suggesting ? [...rest, label] : rest };
    }),
  updateGoal: noop,
  keepGoal: noop,
  acceptGoal: noop,
  keepCode: noop,
  setCodeEditedGoalActions: (actions) => set(actions),
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

export function blockIsBuilding(
  state: Pick<CopilotActionStore, "generatingBlockLabel" | "queuedBuilds">,
  label: string,
): boolean {
  return (
    state.generatingBlockLabel === label ||
    state.queuedBuilds.some((queued) => queued.blockLabel === label)
  );
}

// The one rule for whether a Goal action on a block may run; buttons disable on it and the canvas
// refuses on it.
export function goalActionIsLocked(
  state: Pick<CopilotActionStore, "generatingBlockLabel" | "queuedBuilds">,
  label: string,
  { readOnly, mutationLocked }: { readOnly: boolean; mutationLocked: boolean },
): boolean {
  return readOnly || mutationLocked || blockIsBuilding(state, label);
}
