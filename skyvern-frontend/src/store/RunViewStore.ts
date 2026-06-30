import { create } from "zustand";

// Which tab takes over the hero center. "default" leaves the live stream /
// recording / screenshot logic in charge; the rest are explicit center tabs.
export type RunCenterView = "default" | "code" | "inputs" | "outputs";

type RunViewState = {
  // The frame the user is inspecting. null means "follow the live edge" while
  // running, or the recording once finalized.
  pinnedFrameId: string | null;
  centerView: RunCenterView;
  // True when the run-tab header is too narrow for labels (set by RunHero); the
  // toggles and externally-rendered dropdown triggers then collapse to icons.
  headerCompact: boolean;
  pinFrame: (id: string) => void;
  jumpToLive: () => void;
  setCenterView: (view: RunCenterView) => void;
  setHeaderCompact: (compact: boolean) => void;
  reset: () => void;
};

export const useRunViewStore = create<RunViewState>((set) => ({
  pinnedFrameId: null,
  centerView: "default",
  headerCompact: false,
  // Inspecting a frame or jumping to live/recording always drops any override.
  pinFrame: (id) => set({ pinnedFrameId: id, centerView: "default" }),
  jumpToLive: () => set({ pinnedFrameId: null, centerView: "default" }),
  setCenterView: (view) => set({ centerView: view }),
  setHeaderCompact: (compact) => set({ headerCompact: compact }),
  reset: () => set({ pinnedFrameId: null, centerView: "default" }),
}));
