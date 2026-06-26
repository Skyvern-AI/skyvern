import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

export type StudioTab = "editor" | "browser" | "run";

type StudioShellState = {
  tab: StudioTab;
  copilotCollapsed: boolean;
  pipMinimized: boolean;
  setTab: (tab: StudioTab) => void;
  setCopilotCollapsed: (collapsed: boolean) => void;
  toggleCopilot: () => void;
  setPipMinimized: (minimized: boolean) => void;
  togglePip: () => void;
  reset: () => void;
};

const DEFAULTS = {
  tab: "editor" as StudioTab,
  copilotCollapsed: false,
  pipMinimized: false,
};

export const STUDIO_SHELL_STORAGE_KEY = "skyvern.studioShell";

export const useStudioShellStore = create<StudioShellState>()(
  persist(
    (set) => ({
      ...DEFAULTS,
      setTab: (tab) => set({ tab }),
      setCopilotCollapsed: (copilotCollapsed) => set({ copilotCollapsed }),
      toggleCopilot: () =>
        set((state) => ({ copilotCollapsed: !state.copilotCollapsed })),
      setPipMinimized: (pipMinimized) => set({ pipMinimized }),
      togglePip: () => set((state) => ({ pipMinimized: !state.pipMinimized })),
      reset: () => set(DEFAULTS),
    }),
    {
      name: STUDIO_SHELL_STORAGE_KEY,
      storage: createJSONStorage(() => localStorage),
      // `tab` is intentionally not persisted: the Run tab is gated on hasRun,
      // so each session starts on the editor.
      partialize: (state) => ({
        copilotCollapsed: state.copilotCollapsed,
        pipMinimized: state.pipMinimized,
      }),
    },
  ),
);
