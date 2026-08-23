import { createContext, useContext } from "react";

import { DEFAULT_STUDIO_PANES, type StudioPaneId } from "./panes";

export type PaneClamp = {
  // Runtime-selected panes and their fitting prefix.
  source: readonly StudioPaneId[];
  presented: readonly StudioPaneId[];
  // URL/default panes stay independent from runtime Copilot selection.
  urlSource: readonly StudioPaneId[];
  urlPresented: readonly StudioPaneId[];
};

export type PaneWrite = {
  previous: readonly StudioPaneId[];
  next: readonly StudioPaneId[];
  // A write that changes only Copilot updates the runtime clamp without
  // exposing panes that the mount-time viewport clamp hid.
  nextRuntimeSource?: readonly StudioPaneId[];
};

export type StudioPaneDefaultsValue = {
  defaultPanes: readonly StudioPaneId[];
  clamp: PaneClamp | null;
  notePaneWrite: (change: PaneWrite) => void;
  registerStageElement: (el: HTMLElement | null) => void;
  learnedRunPanes: readonly StudioPaneId[] | null;
};

const noop = () => undefined;

// Module default keeps useStudioPanes safe outside the studio shell (legacy
// Workspace, tests): legacy default panes, no clamping, no nudges.
export const StudioPaneDefaultsContext = createContext<StudioPaneDefaultsValue>(
  {
    defaultPanes: DEFAULT_STUDIO_PANES,
    clamp: null,
    notePaneWrite: noop,
    registerStageElement: noop,
    learnedRunPanes: null,
  },
);

export function useStudioPaneDefaults(): StudioPaneDefaultsValue {
  return useContext(StudioPaneDefaultsContext);
}
