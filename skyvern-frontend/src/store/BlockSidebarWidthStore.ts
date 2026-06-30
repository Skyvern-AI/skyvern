import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

export const BLOCK_SIDEBAR_WIDTH_MIN = 320;
export const BLOCK_SIDEBAR_WIDTH_MAX = 640;
// Matches the Copilot column (STUDIO_COPILOT_WIDTH) so the two studio rails open
// to the same width; still drag-resizable within MIN/MAX.
export const BLOCK_SIDEBAR_WIDTH_DEFAULT = 450;
// The pre-v1 default; v0 sessions that never customized the width hold this, and
// should move to the new default rather than keep the old narrower one.
const BLOCK_SIDEBAR_WIDTH_OLD_DEFAULT = 360;
export const BLOCK_SIDEBAR_WIDTH_STORAGE_KEY = "skyvern.blockSidebarWidth";

type BlockSidebarWidthStore = {
  width: number;
  renderedWidth: number;
  setWidth: (next: number) => void;
  setRenderedWidth: (next: number) => void;
  reset: () => void;
};

function clamp(n: number): number {
  if (Number.isNaN(n)) return BLOCK_SIDEBAR_WIDTH_DEFAULT;
  return Math.min(
    BLOCK_SIDEBAR_WIDTH_MAX,
    Math.max(BLOCK_SIDEBAR_WIDTH_MIN, Math.round(n)),
  );
}

function normalizeRenderedWidth(n: number): number {
  if (!Number.isFinite(n)) return BLOCK_SIDEBAR_WIDTH_DEFAULT;
  return Math.max(0, n);
}

const useBlockSidebarWidthStore = create<BlockSidebarWidthStore>()(
  persist(
    (set) => ({
      width: BLOCK_SIDEBAR_WIDTH_DEFAULT,
      renderedWidth: BLOCK_SIDEBAR_WIDTH_DEFAULT,
      setWidth: (next) => set({ width: clamp(next) }),
      setRenderedWidth: (next) =>
        set({ renderedWidth: normalizeRenderedWidth(next) }),
      reset: () =>
        set({
          width: BLOCK_SIDEBAR_WIDTH_DEFAULT,
          renderedWidth: BLOCK_SIDEBAR_WIDTH_DEFAULT,
        }),
    }),
    {
      name: BLOCK_SIDEBAR_WIDTH_STORAGE_KEY,
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({ width: state.width }),
      // Bumped when the default changed 360 -> 450 (match the Copilot column).
      // Move the old default (and any out-of-range value) to the new default,
      // but keep a width the user deliberately dragged within range.
      version: 1,
      migrate: (persisted) => {
        const prev = (persisted as { width?: number } | null)?.width;
        const keep =
          typeof prev === "number" &&
          prev !== BLOCK_SIDEBAR_WIDTH_OLD_DEFAULT &&
          prev >= BLOCK_SIDEBAR_WIDTH_MIN &&
          prev <= BLOCK_SIDEBAR_WIDTH_MAX;
        return { width: keep ? prev : BLOCK_SIDEBAR_WIDTH_DEFAULT };
      },
    },
  ),
);

export { useBlockSidebarWidthStore };
