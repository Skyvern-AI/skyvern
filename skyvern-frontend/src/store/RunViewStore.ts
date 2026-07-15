import { create } from "zustand";

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
  pinFrame: (id: string, iteration?: number | null) => void;
  jumpToLive: () => void;
  reset: () => void;
};

export const useRunViewStore = create<RunViewState>((set) => ({
  pinnedFrameId: null,
  pinNonce: 0,
  activeIteration: null,
  pinFrame: (id, iteration = null) =>
    set((state) => ({
      pinnedFrameId: id,
      activeIteration: iteration,
      pinNonce: state.pinNonce + 1,
    })),
  jumpToLive: () => set({ pinnedFrameId: null, activeIteration: null }),
  reset: () => set({ pinnedFrameId: null, activeIteration: null }),
}));
