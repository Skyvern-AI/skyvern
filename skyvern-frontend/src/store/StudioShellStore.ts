import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

import { sanitizePaneWidth, type PaneWidths } from "@/store/paneWidths";

type StudioShellState = {
  pipMinimized: boolean;
  // User-pinned pane widths (px) from divider drags; per user, NOT in the URL
  // so shared ?panes= links never impose someone else's widths.
  paneWidths: PaneWidths;
  setPipMinimized: (minimized: boolean) => void;
  togglePip: () => void;
  setPaneWidths: (widths: PaneWidths) => void;
  resetPaneWidths: () => void;
  reset: () => void;
};

const DEFAULTS = {
  pipMinimized: false,
  paneWidths: {} as PaneWidths,
};

export const STUDIO_SHELL_STORAGE_KEY = "skyvern.studioShell";

function sanitizePaneWidths(raw: unknown): PaneWidths {
  if (raw === null || typeof raw !== "object") {
    return {};
  }
  const result: PaneWidths = {};
  for (const [key, value] of Object.entries(raw)) {
    const width = sanitizePaneWidth(value);
    if (width !== undefined) {
      result[key] = width;
    }
  }
  return result;
}

export const useStudioShellStore = create<StudioShellState>()(
  persist(
    (set) => ({
      ...DEFAULTS,
      setPipMinimized: (pipMinimized) => set({ pipMinimized }),
      togglePip: () => set((state) => ({ pipMinimized: !state.pipMinimized })),
      setPaneWidths: (widths) =>
        set((state) => ({
          paneWidths: sanitizePaneWidths({ ...state.paneWidths, ...widths }),
        })),
      resetPaneWidths: () => set({ paneWidths: {} }),
      reset: () => set(DEFAULTS),
    }),
    {
      name: STUDIO_SHELL_STORAGE_KEY,
      storage: createJSONStorage(() => localStorage),
      // v0 also persisted copilotCollapsed; pane visibility now lives in the URL.
      version: 1,
      migrate: (persisted) => {
        const state = persisted as
          | { pipMinimized?: unknown; paneWidths?: unknown }
          | undefined;
        return {
          pipMinimized: Boolean(state?.pipMinimized),
          paneWidths: sanitizePaneWidths(state?.paneWidths),
        };
      },
      partialize: (state) => ({
        pipMinimized: state.pipMinimized,
        paneWidths: state.paneWidths,
      }),
    },
  ),
);
