import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

import {
  type StudioLayoutClass,
  type StudioPaneId,
} from "@/routes/workflows/studio/panes";
import { sanitizePaneWidth, type PaneWidths } from "@/store/paneWidths";

type StudioShellState = {
  pipMinimized: boolean;
  // User-pinned pane widths (px) from divider drags; per user, NOT in the URL
  // so shared ?panes= links never impose someone else's widths.
  paneWidths: PaneWidths;
  // Last user-gesture pane list per layout class; used to restore defaults.
  paneLayouts: Partial<Record<StudioLayoutClass, StudioPaneId[]>>;
  setPipMinimized: (minimized: boolean) => void;
  togglePip: () => void;
  setPaneWidths: (widths: PaneWidths) => void;
  resetPaneWidths: () => void;
  setPaneLayout: (
    cls: StudioLayoutClass,
    panes: readonly StudioPaneId[],
  ) => void;
  reset: () => void;
};

const DEFAULTS = {
  pipMinimized: false,
  paneWidths: {} as PaneWidths,
  paneLayouts: {} as Partial<Record<StudioLayoutClass, StudioPaneId[]>>,
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
      setPaneLayout: (cls, panes) => {
        if (panes.length === 0) return;
        set((state) => ({
          paneLayouts: { ...state.paneLayouts, [cls]: [...panes] },
        }));
      },
      reset: () => set(DEFAULTS),
    }),
    {
      name: STUDIO_SHELL_STORAGE_KEY,
      storage: createJSONStorage(() => localStorage),
      // v0 also persisted copilotCollapsed; pane visibility now lives in the URL.
      // v2 adds paneLayouts for per-class layout memory.
      version: 2,
      migrate: (persisted) => {
        const state = persisted as
          | {
              pipMinimized?: unknown;
              paneWidths?: unknown;
              paneLayouts?: unknown;
            }
          | undefined;
        return {
          pipMinimized: Boolean(state?.pipMinimized),
          paneWidths: sanitizePaneWidths(state?.paneWidths),
          // Inner pane id arrays are not sanitized here; sanitizeLearnedPanes
          // in StudioPaneDefaults.tsx drops stale ids at read time.
          paneLayouts:
            state?.paneLayouts !== null &&
            typeof state?.paneLayouts === "object"
              ? (state.paneLayouts as Partial<
                  Record<StudioLayoutClass, StudioPaneId[]>
                >)
              : {},
        };
      },
      partialize: (state) => ({
        pipMinimized: state.pipMinimized,
        paneWidths: state.paneWidths,
        paneLayouts: state.paneLayouts,
      }),
    },
  ),
);
