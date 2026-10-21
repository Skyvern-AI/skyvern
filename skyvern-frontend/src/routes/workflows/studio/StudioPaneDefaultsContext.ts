import { createContext, useContext } from "react";

import { type PaneWidths } from "@/store/paneWidths";
import { DEFAULT_STUDIO_PANES, type StudioPaneId } from "./panes";

// Panes trimmed from a fuller arrangement; the dropped ones keep their slots
// in it, so reopening one puts it back where it was.
export type PanesCutFrom = {
  panes: StudioPaneId[];
  arrangement: readonly StudioPaneId[];
};

export type StudioPaneDefaultsValue = {
  isStudio: boolean;
  panes: readonly StudioPaneId[];
  paneWidths: PaneWidths;
  entryId: number;
  getPanes: () => readonly StudioPaneId[];
  updatePanes: (
    compute: (
      panes: StudioPaneId[],
      slots: readonly StudioPaneId[],
      stageWidth: number,
    ) => StudioPaneId[] | PanesCutFrom,
    // layout: the user picked the whole pane set, so leaving Editor out of it
    // counts as closing Editor even when it was already closed.
    options?: { byUser?: boolean; layout?: boolean },
  ) => void;
  // Opens Editor unless the user closed it during this visit.
  reopenEditor: () => void;
  setPaneWidths: (widths: PaneWidths) => void;
  resetPaneWidths: () => void;
  preserveNextEntry: (
    search: string | null,
    panes?: readonly StudioPaneId[],
  ) => void;
  registerStageElement: (el: HTMLElement | null) => void;
};

const noop = () => undefined;

export const StudioPaneDefaultsContext = createContext<StudioPaneDefaultsValue>(
  {
    isStudio: false,
    panes: DEFAULT_STUDIO_PANES,
    paneWidths: {},
    entryId: 0,
    getPanes: () => DEFAULT_STUDIO_PANES,
    updatePanes: noop,
    reopenEditor: noop,
    setPaneWidths: noop,
    resetPaneWidths: noop,
    preserveNextEntry: noop,
    registerStageElement: noop,
  },
);

export function useStudioPaneDefaults(): StudioPaneDefaultsValue {
  return useContext(StudioPaneDefaultsContext);
}
