import { createContext, useContext } from "react";

type StudioShellContextValue = {
  // Left-column target Workspace portals the docked Copilot into; null when
  // collapsed or when Workspace isn't embedded in the studio shell.
  copilotPortalEl: HTMLElement | null;
  // Tabs register their stream container so the shell can re-parent the single
  // persistent stream node into the active surface without remounting it.
  setEditorStreamSlot: (el: HTMLElement | null) => void;
  setBrowserStreamSlot: (el: HTMLElement | null) => void;
};

export const StudioShellContext = createContext<StudioShellContextValue>({
  copilotPortalEl: null,
  setEditorStreamSlot: () => {},
  setBrowserStreamSlot: () => {},
});

export function useStudioShellContext(): StudioShellContextValue {
  return useContext(StudioShellContext);
}
