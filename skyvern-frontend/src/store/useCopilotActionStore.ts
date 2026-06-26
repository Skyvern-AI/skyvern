import { create } from "zustand";

export type CopilotBlockBuildRequest = {
  blockLabel: string;
  prompt: string;
};

interface CopilotActionStore {
  // A pending request for the copilot to (re)build a single code block from its prompt.
  pendingBuild: CopilotBlockBuildRequest | null;
  // Label of the block currently generating, so the block can show a local busy state.
  generatingBlockLabel: string | null;
  // Bumped when the user stops an in-flight block generation.
  cancelNonce: number;
  requestBuild: (request: CopilotBlockBuildRequest) => void;
  clearPendingBuild: () => void;
  finishGenerating: () => void;
  requestCancel: () => void;
}

export const useCopilotActionStore = create<CopilotActionStore>((set) => ({
  pendingBuild: null,
  generatingBlockLabel: null,
  cancelNonce: 0,
  requestBuild: (request) =>
    set({ pendingBuild: request, generatingBlockLabel: request.blockLabel }),
  clearPendingBuild: () => set({ pendingBuild: null }),
  finishGenerating: () => set({ generatingBlockLabel: null }),
  requestCancel: () =>
    set((state) => ({
      pendingBuild: null,
      generatingBlockLabel: null,
      cancelNonce: state.cancelNonce + 1,
    })),
}));
