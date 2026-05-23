import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

export const BLOCK_SIDEBAR_WIDTH_MIN = 320;
export const BLOCK_SIDEBAR_WIDTH_MAX = 640;
export const BLOCK_SIDEBAR_WIDTH_DEFAULT = 360;
export const BLOCK_SIDEBAR_WIDTH_STORAGE_KEY = "skyvern.blockSidebarWidth";

type BlockSidebarWidthStore = {
  width: number;
  setWidth: (next: number) => void;
  reset: () => void;
};

function clamp(n: number): number {
  if (Number.isNaN(n)) return BLOCK_SIDEBAR_WIDTH_DEFAULT;
  return Math.min(
    BLOCK_SIDEBAR_WIDTH_MAX,
    Math.max(BLOCK_SIDEBAR_WIDTH_MIN, Math.round(n)),
  );
}

const useBlockSidebarWidthStore = create<BlockSidebarWidthStore>()(
  persist(
    (set) => ({
      width: BLOCK_SIDEBAR_WIDTH_DEFAULT,
      setWidth: (next) => set({ width: clamp(next) }),
      reset: () => set({ width: BLOCK_SIDEBAR_WIDTH_DEFAULT }),
    }),
    {
      name: BLOCK_SIDEBAR_WIDTH_STORAGE_KEY,
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({ width: state.width }),
    },
  ),
);

export { useBlockSidebarWidthStore };
