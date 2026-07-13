import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

type StudioFirstRunState = {
  coachMarkSeen: boolean;
  narrowNudgeSeen: boolean;
  markCoachMarkSeen: () => void;
  markNarrowNudgeSeen: () => void;
};

const DEFAULTS = {
  coachMarkSeen: false,
  narrowNudgeSeen: false,
};

export const STUDIO_FIRST_RUN_STORAGE_KEY = "skyvern.studioFirstRun";

export const useStudioFirstRunStore = create<StudioFirstRunState>()(
  persist(
    (set) => ({
      ...DEFAULTS,
      markCoachMarkSeen: () => set({ coachMarkSeen: true }),
      markNarrowNudgeSeen: () => set({ narrowNudgeSeen: true }),
    }),
    {
      name: STUDIO_FIRST_RUN_STORAGE_KEY,
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({
        coachMarkSeen: state.coachMarkSeen,
        narrowNudgeSeen: state.narrowNudgeSeen,
      }),
    },
  ),
);
