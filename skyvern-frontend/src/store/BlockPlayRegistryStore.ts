import { create } from "zustand";

export type BlockPlayHandle = {
  play: () => void;
  stop: () => void;
  // Run is not available right now (run active, no debug session, save paused).
  inert: boolean;
  // This block is the one currently running.
  playing: boolean;
  // A stop request is already in flight.
  stopping: boolean;
  // The canvas header wins over the Doc's headless host when both exist for a node.
  source: "canvas" | "doc";
};

type BlockPlayRegistryState = {
  handles: Record<string, BlockPlayHandle>;
  register: (nodeId: string, handle: BlockPlayHandle) => void;
  unregister: (nodeId: string, source: BlockPlayHandle["source"]) => void;
};

/**
 * The canvas block header owns the single-block run (credential gates, parameter
 * prompts, save-first). Other surfaces, like the Doc, start it through this
 * registry instead of reimplementing those gates.
 */
export const useBlockPlayRegistry = create<BlockPlayRegistryState>()((set) => ({
  handles: {},
  register: (nodeId, handle) =>
    set((state) => ({ handles: { ...state.handles, [nodeId]: handle } })),
  unregister: (nodeId, source) =>
    set((state) => {
      if (state.handles[nodeId]?.source !== source) return state;
      const handles = { ...state.handles };
      delete handles[nodeId];
      return { handles };
    }),
}));
