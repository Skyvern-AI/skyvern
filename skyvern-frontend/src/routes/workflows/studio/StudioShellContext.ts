import { createContext, useContext } from "react";

type StudioShellContextValue = {
  // Left-column target Workspace portals the docked Copilot into; null when
  // collapsed or when Workspace isn't embedded in the studio shell.
  copilotPortalEl: HTMLElement | null;
  // Tabs register their stream container so the shell can re-parent the single
  // persistent stream node into the active surface without remounting it.
  setEditorStreamSlot: (el: HTMLElement | null) => void;
  setBrowserStreamSlot: (el: HTMLElement | null) => void;
  // The Run tab registers this for a block run, so the debug-session stream shows
  // there too (same node, view-only); null for a full run keeps it parked.
  setRunStreamSlot: (el: HTMLElement | null) => void;
};

export const StudioShellContext = createContext<StudioShellContextValue>({
  copilotPortalEl: null,
  setEditorStreamSlot: () => {},
  setBrowserStreamSlot: () => {},
  setRunStreamSlot: () => {},
});

export function useStudioShellContext(): StudioShellContextValue {
  return useContext(StudioShellContext);
}
