import { create } from "zustand";

type SelectedAgentStepCall = {
  runId: string | undefined;
  workflowRunBlockId: string;
  index: number;
};

type RunViewState = {
  // The frame the user is inspecting. null means "follow the live edge" while
  // running, or the final frame once finalized.
  pinnedFrameId: string | null;
  // Bumped on every pinFrame so the Browser pane can react to a re-pin of the
  // already-selected frame (the id alone wouldn't change).
  pinNonce: number;
  // Loop-iteration scope of a pinned container block. Not carried in ?active=,
  // so the Browser pane reads it here to resolve the iteration's screenshot.
  activeIteration: number | null;
  // Set only by the pin that selected it, so any later pin or unpin clears it.
  selectedAgentStepCall: SelectedAgentStepCall | null;
  pinFrame: (
    id: string,
    iteration?: number | null,
    agentStepCall?: SelectedAgentStepCall | null,
  ) => void;
  clearAgentStepCall: () => void;
  jumpToLive: () => void;
  reset: () => void;
};

export const useRunViewStore = create<RunViewState>((set) => ({
  pinnedFrameId: null,
  pinNonce: 0,
  activeIteration: null,
  selectedAgentStepCall: null,
  pinFrame: (id, iteration = null, agentStepCall = null) =>
    set((state) => ({
      pinnedFrameId: id,
      activeIteration: iteration,
      pinNonce: state.pinNonce + 1,
      selectedAgentStepCall: agentStepCall,
    })),
  clearAgentStepCall: () => set({ selectedAgentStepCall: null }),
  jumpToLive: () =>
    set({
      pinnedFrameId: null,
      activeIteration: null,
      selectedAgentStepCall: null,
    }),
  reset: () =>
    set({
      pinnedFrameId: null,
      activeIteration: null,
      selectedAgentStepCall: null,
    }),
}));
