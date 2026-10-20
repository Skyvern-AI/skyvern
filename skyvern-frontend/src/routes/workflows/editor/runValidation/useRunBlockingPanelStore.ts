import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

// Whether the run-blocking panel is collapsed to its compact pill. Persisted so
// a user who tucks it away keeps it tucked away across reloads; defaults open so
// a first-time blocking state is never hidden.
type RunBlockingPanelStore = {
  collapsed: boolean;
  setCollapsed: (collapsed: boolean) => void;
};

export const useRunBlockingPanelStore = create<RunBlockingPanelStore>()(
  persist(
    (set) => ({
      collapsed: false,
      setCollapsed: (collapsed) => set({ collapsed }),
    }),
    {
      name: "skyvern:run-blocking-panel-collapsed",
      storage: createJSONStorage(() => localStorage),
      version: 1,
    },
  ),
);
