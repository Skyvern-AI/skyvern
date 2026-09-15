import { create } from "zustand";

import type { RecordingEvidencePacket } from "@/routes/workflows/copilot/workflowCopilotTypes";

type ArmedEvidence = {
  nonce: string;
  evidence: RecordingEvidencePacket;
};

type RecordingRefinementEvidenceStore = {
  armed: ArmedEvidence | null;
  set: (armed: ArmedEvidence) => void;
  peek: (nonce: string) => RecordingEvidencePacket | null;
  take: (nonce: string) => RecordingEvidencePacket | null;
};

/**
 * Hand-off for the multi-KB evidence packet the refine_recording copilot turn posts.
 * Router `location.state` carries only the nonce that names the packet held here.
 */
const useRecordingRefinementEvidenceStore =
  create<RecordingRefinementEvidenceStore>((set, get) => ({
    armed: null,
    set: (armed) => set({ armed }),
    peek: (nonce) => {
      const armed = get().armed;
      return armed?.nonce === nonce ? armed.evidence : null;
    },
    take: (nonce) => {
      const armed = get().armed;
      if (armed?.nonce !== nonce) {
        return null;
      }
      set({ armed: null });
      return armed.evidence;
    },
  }));

export { useRecordingRefinementEvidenceStore };
export type { ArmedEvidence };
