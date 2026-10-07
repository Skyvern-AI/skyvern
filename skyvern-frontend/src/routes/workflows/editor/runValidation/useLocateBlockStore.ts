import { create } from "zustand";

// Fulfilled by FlowRenderer under its pan-constraint guard, so locate works in pan-locked browser/debug mode.
type LocateRequest = {
  nodeId: string;
  // Bumped per request so locating the same block twice still re-fires the effect.
  nonce: number;
};

type LocateBlockStore = {
  request: LocateRequest | null;
  requestLocate: (nodeId: string) => void;
  clearLocate: () => void;
  // Set once the located block has been revealed + panned into view; drives a
  // one-shot glow on the target so the eye lands on it. Cleared on a timer.
  pulseNodeId: string | null;
  startPulse: (nodeId: string) => void;
  clearPulse: () => void;
};

export const useLocateBlockStore = create<LocateBlockStore>((set, get) => ({
  request: null,
  requestLocate: (nodeId) =>
    set({ request: { nodeId, nonce: (get().request?.nonce ?? 0) + 1 } }),
  clearLocate: () => set({ request: null }),
  pulseNodeId: null,
  startPulse: (nodeId) => set({ pulseNodeId: nodeId }),
  clearPulse: () => set({ pulseNodeId: null }),
}));
