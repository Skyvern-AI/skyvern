import type { WorkflowEditorMode } from "./hooks/useWorkflowEditorMode";

export const BLOCK_SIDEBAR_WIDTH_VAR = "--block-sidebar-w";
export const BLOCK_SIDEBAR_RIGHT_GAP = "1.5rem";

export const HEADER_RIGHT_INSET_CLOSED = "right-6";
export const HEADER_RIGHT_INSET_OPEN =
  "right-[calc(var(--block-sidebar-w)+3rem)]";

export function isBlockSidebarOpen(
  mode: WorkflowEditorMode,
  selectedBlockId: string | null,
  // The right rail also renders when the workflow panel is open on the
  // nodeLibrary content (NodeAdder click → "+ block" library). Without
  // this, the header/right-side overlays would anchor to `right-6`
  // while the library is visibly open and slide under the panel.
  // Build mode has no block-config sidebar, so only the library counts.
  isNodeLibraryOpen: boolean = false,
): boolean {
  if (mode === "build") return isNodeLibraryOpen;
  return selectedBlockId !== null || isNodeLibraryOpen;
}
