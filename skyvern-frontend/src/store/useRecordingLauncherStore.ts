import { create } from "zustand";

import { type WorkflowCopilotDemonstrationInsertion } from "@/routes/workflows/copilot/workflowCopilotTypes";

export type StartDemonstrationOptions = {
  insertion?: WorkflowCopilotDemonstrationInsertion;
};

/**
 * Bridges the studio's Browser-tab Record button to the canvas-aware Workspace.
 * The Workspace (which has ReactFlow nodes/edges) registers a launcher that
 * computes the append-at-end insertion point and starts recording; the button,
 * which lives outside the canvas, just invokes it.
 */
type RecordingLauncherState = {
  startRecordingAtEnd: (() => void) | null;
  setStartRecordingAtEnd: (launcher: (() => void) | null) => void;
  startDemonstration: ((options?: StartDemonstrationOptions) => void) | null;
  setStartDemonstration: (
    launcher: ((options?: StartDemonstrationOptions) => void) | null,
  ) => void;
  demonstrationBrowserSessionId: string | null;
  demonstrationRecordingBrowserSessionId: string | null;
  navigateDemonstration: ((url: string) => void) | null;
};

export const useRecordingLauncherStore = create<RecordingLauncherState>(
  (set) => ({
    startRecordingAtEnd: null,
    setStartRecordingAtEnd: (startRecordingAtEnd) =>
      set({ startRecordingAtEnd }),
    startDemonstration: null,
    setStartDemonstration: (startDemonstration) => set({ startDemonstration }),
    demonstrationBrowserSessionId: null,
    demonstrationRecordingBrowserSessionId: null,
    navigateDemonstration: null,
  }),
);
