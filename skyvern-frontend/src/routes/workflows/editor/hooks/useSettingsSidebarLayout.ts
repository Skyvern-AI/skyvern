import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { useStudioShellStore } from "@/store/StudioShellStore";

import { isBlockSidebarOpen } from "../blockSidebar";
import { useWorkflowEditorMode } from "./useWorkflowEditorMode";

type SettingsSidebarLayout = {
  // The settings surface is visible at all (a block/start node is selected, or
  // the block library drawer is open).
  open: boolean;
  // The library drawer (not collapsible) rather than a block's config form.
  isLibrary: boolean;
  // A block/start node is selected, so the panel can collapse to a rail.
  nodeSelected: boolean;
  // The panel is showing as a collapsed rail (only for a selected node).
  collapsed: boolean;
};

// Single source of truth for the studio settings sidebar's layout state, shared
// by StudioShell (which sizes the grid column) and BlockConfigSidebar (which
// renders the body/rail) so the two can't drift.
export function useSettingsSidebarLayout(): SettingsSidebarLayout {
  const mode = useWorkflowEditorMode();
  const selectedBlockId = useWorkflowPanelStore((s) => s.selectedBlockId);
  const workflowPanelState = useWorkflowPanelStore((s) => s.workflowPanelState);
  const studioSettingsCollapsed = useStudioShellStore(
    (s) => s.settingsCollapsed,
  );

  // The comparison view replaces FlowRenderer (and thus the portaled settings
  // panel) in the Stage, so there'd be no content for the column — keep it
  // closed so it doesn't reserve an empty strip that shrinks the comparison.
  const comparisonActive = Boolean(
    workflowPanelState.data?.showComparison &&
    workflowPanelState.data?.version1 &&
    workflowPanelState.data?.version2,
  );
  const isLibrary =
    workflowPanelState.active && workflowPanelState.content === "nodeLibrary";
  const nodeSelected =
    !comparisonActive &&
    mode !== "build" &&
    selectedBlockId !== null &&
    !isLibrary;
  const open =
    !comparisonActive && isBlockSidebarOpen(mode, selectedBlockId, isLibrary);
  const collapsed = nodeSelected && studioSettingsCollapsed;

  return { open, isLibrary, nodeSelected, collapsed };
}
