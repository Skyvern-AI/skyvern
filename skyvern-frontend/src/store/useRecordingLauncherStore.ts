import { create } from "zustand";

import { type WorkflowCopilotDemonstrationInsertion } from "@/routes/workflows/copilot/workflowCopilotTypes";

export function noteDemonstrationStartFailed() {
  useRecordingLauncherStore.setState((state) => ({
    demonstrationStartFailures: state.demonstrationStartFailures + 1,
  }));
}

export type StartDemonstrationOptions = {
  insertion?: WorkflowCopilotDemonstrationInsertion;
  // The Doc stays open beside the Browser; the canvas steps aside for it.
  keepEditor?: boolean;
};

// What the Doc draws at the insertion point while a recording is open.
export type DemonstrationStatus = {
  phase: "recording" | "stopped";
  actionCount: number;
  interrupted: boolean;
  insertion: WorkflowCopilotDemonstrationInsertion | null;
};

type RecordingLauncherState = {
  startDemonstration: ((options?: StartDemonstrationOptions) => void) | null;
  setStartDemonstration: (
    launcher: ((options?: StartDemonstrationOptions) => void) | null,
  ) => void;
  // Counts starts that were refused or failed before a recording opened, so a caller waiting on one can stop waiting.
  demonstrationStartFailures: number;
  demonstrationBrowserSessionId: string | null;
  demonstrationRecordingBrowserSessionId: string | null;
  navigateDemonstration: ((url: string) => void) | null;
  demonstration: DemonstrationStatus | null;
};

export const useRecordingLauncherStore = create<RecordingLauncherState>(
  (set) => ({
    startDemonstration: null,
    setStartDemonstration: (startDemonstration) => set({ startDemonstration }),
    demonstrationStartFailures: 0,
    demonstrationBrowserSessionId: null,
    demonstrationRecordingBrowserSessionId: null,
    navigateDemonstration: null,
    demonstration: null,
  }),
);
