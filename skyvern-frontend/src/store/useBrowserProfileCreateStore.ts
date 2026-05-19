import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

type ActiveCreatePhase = "closing" | "waiting" | "creating";

type ActiveBrowserProfileCreate = {
  browserSessionId: string;
  name: string;
  description?: string;
  startTime: number;
  phase: ActiveCreatePhase;
};

type BrowserProfileCreateStore = {
  active: ActiveBrowserProfileCreate | null;
  setActive: (active: ActiveBrowserProfileCreate) => void;
  clearActive: () => void;
};

export type { ActiveBrowserProfileCreate, ActiveCreatePhase };

export const useBrowserProfileCreateStore = create<BrowserProfileCreateStore>()(
  persist(
    (set) => ({
      active: null,
      setActive: (active) => set({ active }),
      clearActive: () => set({ active: null }),
    }),
    {
      name: "browser-profile-create",
      storage: createJSONStorage(() => sessionStorage),
    },
  ),
);
