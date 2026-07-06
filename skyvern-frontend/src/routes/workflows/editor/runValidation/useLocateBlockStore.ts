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
};

export const useLocateBlockStore = create<LocateBlockStore>((set, get) => ({
  request: null,
  requestLocate: (nodeId) =>
    set({ request: { nodeId, nonce: (get().request?.nonce ?? 0) + 1 } }),
  clearLocate: () => set({ request: null }),
}));
