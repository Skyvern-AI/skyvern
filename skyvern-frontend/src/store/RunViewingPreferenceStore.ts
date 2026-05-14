import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

export type RunViewMode = "compact" | "detailed";

type RunViewingPreferenceState = {
  viewMode: RunViewMode;
  setViewMode: (mode: RunViewMode) => void;
  reset: () => void;
};

const DEFAULT_VIEW_MODE: RunViewMode = "compact";

export const RUN_VIEWING_STORAGE_KEY = "skyvern.runViewing";

export const useRunViewingPreferenceStore = create<RunViewingPreferenceState>()(
  persist(
    (set) => ({
      viewMode: DEFAULT_VIEW_MODE,
      setViewMode: (mode) => set({ viewMode: mode }),
      reset: () => {
        set({ viewMode: DEFAULT_VIEW_MODE });
        useRunViewingPreferenceStore.persist.clearStorage();
      },
    }),
    {
      name: RUN_VIEWING_STORAGE_KEY,
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({ viewMode: state.viewMode }),
    },
  ),
);
