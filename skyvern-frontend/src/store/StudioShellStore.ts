import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

type StudioShellState = {
  pipMinimized: boolean;
  setPipMinimized: (minimized: boolean) => void;
  togglePip: () => void;
  reset: () => void;
};

const DEFAULTS = {
  pipMinimized: false,
};

export const STUDIO_SHELL_STORAGE_KEY = "skyvern.studioShell";

export const useStudioShellStore = create<StudioShellState>()(
  persist(
    (set) => ({
      ...DEFAULTS,
      setPipMinimized: (pipMinimized) => set({ pipMinimized }),
      togglePip: () => set((state) => ({ pipMinimized: !state.pipMinimized })),
      reset: () => set(DEFAULTS),
    }),
    {
      name: STUDIO_SHELL_STORAGE_KEY,
      storage: createJSONStorage(() => localStorage),
      // v0 also persisted copilotCollapsed; pane visibility now lives in the URL.
      version: 1,
      migrate: (persisted) => ({
        pipMinimized: Boolean(
          (persisted as { pipMinimized?: unknown } | undefined)?.pipMinimized,
        ),
      }),
      partialize: (state) => ({ pipMinimized: state.pipMinimized }),
    },
  ),
);
