import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

type StudioFirstRunState = {
  narrowNudgeSeen: boolean;
  markNarrowNudgeSeen: () => void;
};

const DEFAULTS = {
  narrowNudgeSeen: false,
};

export const STUDIO_FIRST_RUN_STORAGE_KEY = "skyvern.studioFirstRun";

export const useStudioFirstRunStore = create<StudioFirstRunState>()(
  persist(
    (set) => ({
      ...DEFAULTS,
      markNarrowNudgeSeen: () => set({ narrowNudgeSeen: true }),
    }),
    {
      name: STUDIO_FIRST_RUN_STORAGE_KEY,
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({
        narrowNudgeSeen: state.narrowNudgeSeen,
      }),
    },
  ),
);
