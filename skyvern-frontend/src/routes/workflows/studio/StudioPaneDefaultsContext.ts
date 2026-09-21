import { createContext, useContext } from "react";

import { type PaneWidths } from "@/store/paneWidths";
import { DEFAULT_STUDIO_PANES, type StudioPaneId } from "./panes";

export type StudioPaneDefaultsValue = {
  isStudio: boolean;
  panes: readonly StudioPaneId[];
  paneWidths: PaneWidths;
  entryId: number;
  getPanes: () => readonly StudioPaneId[];
  updatePanes: (compute: (panes: StudioPaneId[]) => StudioPaneId[]) => void;
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
    setPaneWidths: noop,
    resetPaneWidths: noop,
    preserveNextEntry: noop,
    registerStageElement: noop,
  },
);

export function useStudioPaneDefaults(): StudioPaneDefaultsValue {
  return useContext(StudioPaneDefaultsContext);
}
