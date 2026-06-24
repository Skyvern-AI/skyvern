import { create } from "zustand";

/**
 * Bridges the studio's single live-browser stream and the Browser tab's chrome:
 * the stream publishes its URL here; the toolbar drives a reconnect back into it.
 */
type StudioBrowserState = {
  streamUrl: string | null;
  reloadNonce: number;
  setStreamUrl: (url: string | null) => void;
  reload: () => void;
  reset: () => void;
};

export const useStudioBrowserStore = create<StudioBrowserState>()((set) => ({
  streamUrl: null,
  reloadNonce: 0,
  setStreamUrl: (streamUrl) => set({ streamUrl }),
  reload: () => set((state) => ({ reloadNonce: state.reloadNonce + 1 })),
  reset: () => set({ streamUrl: null }),
}));
