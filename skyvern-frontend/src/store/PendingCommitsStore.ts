import { create } from "zustand";

type CommitFn = () => boolean;

type PendingCommitsStore = {
  commits: Record<string, CommitFn>;
  register: (blockId: string, commit: CommitFn) => void;
  unregister: (blockId: string) => void;
  flush: (blockId: string | null) => boolean;
};

// Registration channel for the switching-blocks auto-commit hook. The
// dispatcher calls `register(blockId, commit)` inside each per-type config
// form so the sidebar can flush the previous block's pending edits when
// `selectedBlockId` changes. Keeping this store independent of
// `useDebouncedSidebarSave` lets the dispatcher swap commit semantics
// without touching the hook.
const usePendingCommitsStore = create<PendingCommitsStore>((set, get) => {
  return {
    commits: {},
    register: (blockId: string, commit: CommitFn) => {
      set((state) => ({
        commits: { ...state.commits, [blockId]: commit },
      }));
    },
    unregister: (blockId: string) => {
      set((state) => {
        if (!(blockId in state.commits)) {
          return state;
        }
        const next = { ...state.commits };
        delete next[blockId];
        return { commits: next };
      });
    },
    flush: (blockId: string | null) => {
      if (blockId === null) {
        return true;
      }
      const commit = get().commits[blockId];
      if (!commit) {
        return true;
      }
      return commit();
    },
  };
});

export { usePendingCommitsStore, type CommitFn };
