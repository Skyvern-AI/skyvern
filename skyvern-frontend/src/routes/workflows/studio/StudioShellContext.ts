import { createContext, useContext } from "react";

import { type StudioPaneId } from "./panes";

type StudioShellContextValue = {
  organizationId?: string | null;
  // Left-column target Workspace portals the docked Copilot into; null when
  // collapsed or when Workspace isn't embedded in the studio shell.
  copilotPortalEl: HTMLElement | null;
  // Tabs register their stream container so the shell can re-parent the single
  // persistent stream node into the active surface without remounting it.
  setEditorStreamSlot: (el: HTMLElement | null) => void;
  setBrowserStreamSlot: (el: HTMLElement | null) => void;
  // The Overview pane registers this for a block run, so the debug-session stream shows
  // there too (same node, view-only); null for a full run keeps it parked.
  setRunStreamSlot: (el: HTMLElement | null) => void;
  // Pane actions from nested controls leave fullscreen before applying their
  // own layout change, including when the requested pane is already open.
  restoreExpandedPane?: () => void;
  // Brings an open pane on screen: the shown pane at phone width, or opened
  // over the stage when it is folded into a strip to make room for the Doc.
  showPane?: (id: StudioPaneId) => void;
  // Whether an open pane is folded into a strip to make room for the Doc.
  isPaneFolded?: (id: StudioPaneId) => boolean;
  // Whether a pane is on screen now: open, not folded, not hidden at phone width or behind an expanded pane.
  isPaneOnScreen?: (id: StudioPaneId) => boolean;
};

export const StudioShellContext = createContext<StudioShellContextValue>({
  organizationId: null,
  copilotPortalEl: null,
  setEditorStreamSlot: () => {},
  setBrowserStreamSlot: () => {},
  setRunStreamSlot: () => {},
  restoreExpandedPane: () => {},
});

export function useStudioShellContext(): StudioShellContextValue {
  return useContext(StudioShellContext);
}

// True when the hosting pane header is too narrow for labels; header chrome
// (view pills, badges) collapses to icons. Provided by StudioPane.
export const StudioPaneCompactContext = createContext(false);

export function useStudioPaneCompact(): boolean {
  return useContext(StudioPaneCompactContext);
}

// The source agent's deleted_at when the studio shows a run whose workflow was
// deleted (the shell runs from the run's embedded snapshot). Workflow-mutating
// chrome keys off this to degrade: Copilot/Editor panes blocked, top-bar
// actions replaced by the legacy "Agent deleted on …" tag.
export const StudioWorkflowDeletedContext = createContext<string | null>(null);

export function useStudioWorkflowDeletedAt(): string | null {
  return useContext(StudioWorkflowDeletedContext);
}
