import { create } from "zustand";

import type { StreamState } from "@/routes/streaming/streamState";

// "auto" follows the run-aware machine in studio/browserPaneView.ts; the header
// pills pin an explicit view until the next timeline selection or run change.
export type BrowserPaneViewIntent =
  | "auto"
  | "live"
  | "recording"
  | "screenshots";

/**
 * Bridges the studio's single live-browser stream and the Browser pane's chrome:
 * the stream publishes its URL here; the header drives a reconnect back into it
 * and holds the pane's Live/Recording/Screenshots view intent.
 */
type StudioBrowserState = {
  streamUrl: string | null;
  debugStream: { browserSessionId: string | null; state: StreamState } | null;
  // The latest run's own stream, when it runs outside the debug session. Not
  // cleared by reset(): that follows the debug session, and a live run stream
  // doesn't re-emit.
  runStream: { workflowRunId: string; state: StreamState } | null;
  hasUnseenActivity: boolean;
  reloadNonce: number;
  view: BrowserPaneViewIntent;
  setStreamUrl: (url: string | null) => void;
  setDebugStreamState: (
    state: StreamState,
    browserSessionId: string | null,
  ) => void;
  setRunStreamState: (state: StreamState, workflowRunId: string) => void;
  markActivity: () => void;
  clearActivity: () => void;
  reload: () => void;
  setView: (view: BrowserPaneViewIntent) => void;
  reset: () => void;
};

export const useStudioBrowserStore = create<StudioBrowserState>()((set) => ({
  streamUrl: null,
  debugStream: null,
  runStream: null,
  hasUnseenActivity: false,
  reloadNonce: 0,
  view: "auto",
  setStreamUrl: (streamUrl) => set({ streamUrl }),
  setDebugStreamState: (state, browserSessionId) =>
    set({ debugStream: { browserSessionId, state } }),
  setRunStreamState: (state, workflowRunId) =>
    set({ runStream: { workflowRunId, state } }),
  markActivity: () =>
    set((state) =>
      state.hasUnseenActivity ? state : { hasUnseenActivity: true },
    ),
  clearActivity: () =>
    set((state) =>
      state.hasUnseenActivity ? { hasUnseenActivity: false } : state,
    ),
  reload: () => set((state) => ({ reloadNonce: state.reloadNonce + 1 })),
  setView: (view) => set({ view }),
  // Also clears the view intent: workflow navigation resets this store without
  // a selection/run change, so a stale replay pill could otherwise survive.
  reset: () =>
    set({
      streamUrl: null,
      debugStream: null,
      hasUnseenActivity: false,
      view: "auto",
    }),
}));
