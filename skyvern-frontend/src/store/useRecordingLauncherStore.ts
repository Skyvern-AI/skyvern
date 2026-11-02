import { create } from "zustand";

import { type WorkflowCopilotDemonstrationInsertion } from "@/routes/workflows/copilot/workflowCopilotTypes";

export type StartDemonstrationOptions = {
  insertion?: WorkflowCopilotDemonstrationInsertion;
};

type RecordingLauncherState = {
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
    startDemonstration: null,
    setStartDemonstration: (startDemonstration) => set({ startDemonstration }),
    demonstrationBrowserSessionId: null,
    demonstrationRecordingBrowserSessionId: null,
    navigateDemonstration: null,
  }),
);
