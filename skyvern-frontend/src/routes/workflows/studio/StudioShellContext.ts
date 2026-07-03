import { createContext, useContext } from "react";

type StudioShellContextValue = {
  // Left-column target Workspace portals the docked Copilot into; null when
  // collapsed or when Workspace isn't embedded in the studio shell.
  copilotPortalEl: HTMLElement | null;
  // Stage-level overlay target for Workspace-wired panels that must outlive a
  // hidden Editor pane (e.g. the code-cache key/value panel).
  panelPortalEl: HTMLElement | null;
  // Tabs register their stream container so the shell can re-parent the single
  // persistent stream node into the active surface without remounting it.
  setEditorStreamSlot: (el: HTMLElement | null) => void;
  setBrowserStreamSlot: (el: HTMLElement | null) => void;
  // The Overview pane registers this for a block run, so the debug-session stream shows
  // there too (same node, view-only); null for a full run keeps it parked.
  setRunStreamSlot: (el: HTMLElement | null) => void;
};

export const StudioShellContext = createContext<StudioShellContextValue>({
  copilotPortalEl: null,
  panelPortalEl: null,
  setEditorStreamSlot: () => {},
  setBrowserStreamSlot: () => {},
  setRunStreamSlot: () => {},
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
