import { create } from "zustand";

export type RunPaneView = "timeline" | "inputs" | "outputs" | "code";

/**
 * Which body view the studio's Overview pane shows. Lives in a store because
 * the toggles render in the pane header (StudioShell) while the body is
 * RunView; both stay in sync without prop-drilling through the shell.
 */
type RunPaneViewState = {
  view: RunPaneView;
  setView: (view: RunPaneView) => void;
  reset: () => void;
};

export const useRunPaneViewStore = create<RunPaneViewState>((set) => ({
  view: "timeline",
  setView: (view) => set({ view }),
  reset: () => set({ view: "timeline" }),
}));
