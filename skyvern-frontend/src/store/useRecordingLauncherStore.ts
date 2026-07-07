import { create } from "zustand";

/**
 * Bridges the studio's Browser-tab Record button to the canvas-aware Workspace.
 * The Workspace (which has ReactFlow nodes/edges) registers a launcher that
 * computes the append-at-end insertion point and starts recording; the button,
 * which lives outside the canvas, just invokes it.
 */
type RecordingLauncherState = {
  startRecordingAtEnd: (() => void) | null;
  setStartRecordingAtEnd: (launcher: (() => void) | null) => void;
};

export const useRecordingLauncherStore = create<RecordingLauncherState>(
  (set) => ({
    startRecordingAtEnd: null,
    setStartRecordingAtEnd: (startRecordingAtEnd) =>
      set({ startRecordingAtEnd }),
  }),
);
