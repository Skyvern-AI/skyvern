import { create } from "zustand";

type SidebarSaveStateStore = {
  lastUpdatedAt: Record<string, number>;
  setLastUpdatedAt: (blockId: string, ts: number) => void;
  getLastUpdatedAt: (blockId: string | null) => number | null;
  reset: () => void;
};

const useSidebarSaveStateStore = create<SidebarSaveStateStore>((set, get) => {
  return {
    lastUpdatedAt: {},
    setLastUpdatedAt: (blockId, ts) => {
      set((state) => ({
        lastUpdatedAt: { ...state.lastUpdatedAt, [blockId]: ts },
      }));
    },
    getLastUpdatedAt: (blockId) => {
      if (blockId === null) {
        return null;
      }
      return get().lastUpdatedAt[blockId] ?? null;
    },
    reset: () => {
      set({ lastUpdatedAt: {} });
    },
  };
});

export { useSidebarSaveStateStore };
