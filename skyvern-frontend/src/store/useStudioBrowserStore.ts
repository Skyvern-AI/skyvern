import { create } from "zustand";

/**
 * Bridges the studio's single live-browser stream and the Browser tab's chrome:
 * the stream publishes its URL here; the toolbar drives a reconnect back into it.
 */
type StudioBrowserState = {
  streamUrl: string | null;
  hasUnseenActivity: boolean;
  reloadNonce: number;
  setStreamUrl: (url: string | null) => void;
  markActivity: () => void;
  clearActivity: () => void;
  reload: () => void;
  reset: () => void;
};

export const useStudioBrowserStore = create<StudioBrowserState>()((set) => ({
  streamUrl: null,
  hasUnseenActivity: false,
  reloadNonce: 0,
  setStreamUrl: (streamUrl) => set({ streamUrl }),
  markActivity: () =>
    set((state) =>
      state.hasUnseenActivity ? state : { hasUnseenActivity: true },
    ),
  clearActivity: () =>
    set((state) =>
      state.hasUnseenActivity ? { hasUnseenActivity: false } : state,
    ),
  reload: () => set((state) => ({ reloadNonce: state.reloadNonce + 1 })),
  reset: () => set({ streamUrl: null, hasUnseenActivity: false }),
}));
