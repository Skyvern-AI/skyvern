/**
 * A store to hold the scripts for individual blocks in a workflow. As each
 * workflow has uniquely (and differently) labelled blocks, and those labels
 * are block identity, we'll eschew strong typing for this, and use a loose
 * object literal instead.
 */

import { create } from "zustand";

interface BlockScriptStore {
  scriptId?: string;
  scripts: { [k: string]: string };
  // --
  setScript: (blockId: string, script: string) => void;
  setScripts: (scripts: { [k: string]: string }) => void;
  reset: () => void;
}

const useBlockScriptStore = create<BlockScriptStore>((set) => {
  return {
    scriptId: undefined,
    scripts: {},
    // --
    setScript: (blockId: string, script: string) => {
      set((state) => ({
        scripts: {
          ...state.scripts,
          [blockId]: script,
        },
      }));
    },
    setScripts: (scripts: { [k: string]: string }) => {
      set(() => ({
        scripts,
      }));
    },
    reset: () => {
      set({
        scripts: {},
      });
    },
  };
});

export { useBlockScriptStore };
