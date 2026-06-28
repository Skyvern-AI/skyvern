import { create } from "zustand";

type RunViewState = {
  // The frame the user is inspecting. null means "follow the live edge" while
  // running, or the recording once finalized.
  pinnedFrameId: string | null;
  // Whether the generated-code viewer is taking over the hero center.
  codeOpen: boolean;
  pinFrame: (id: string) => void;
  jumpToLive: () => void;
  setCodeOpen: (open: boolean) => void;
  reset: () => void;
};

export const useRunViewStore = create<RunViewState>((set) => ({
  pinnedFrameId: null,
  codeOpen: false,
  // Inspecting a frame or jumping to live/recording always closes the code view.
  pinFrame: (id) => set({ pinnedFrameId: id, codeOpen: false }),
  jumpToLive: () => set({ pinnedFrameId: null, codeOpen: false }),
  setCodeOpen: (open) => set({ codeOpen: open }),
  reset: () => set({ pinnedFrameId: null, codeOpen: false }),
}));
