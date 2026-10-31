import { createContext } from "react";
import type { DocEditApi, DocDestination } from "./docEditApi";

export type DocPendingNote = {
  ownerNodeId: string;
  placement: "above" | "trailing";
};

export const WorkflowDocActionsContext = createContext<{
  editable: boolean;
  runControlsVisible: boolean;
  openSettings: (id: string) => void;
  openVisual: () => void;
  goTo: (id: string) => void;
  selectedBranch: (conditionalId: string) => string | undefined;
  // The step that also reads this step's above note at run time: the one before it in its run sequence.
  previousNoteReader: (nodeId: string) => string | undefined;
  selectBranch: (conditionalId: string, branchId: string) => void;
  loopOpen: (loopId: string) => boolean;
  toggleLoop: (loopId: string) => void;
  // Null when the doc is read-only (flag on but locked, a run going, or a read-only scope).
  edit: DocEditApi | null;
  destinations: () => DocDestination[];
  outputUsers: (
    id: string,
  ) => Array<{ id: string; label: string; text: string }>;
  missingReference: (reference: string) => boolean;
  focusId: string | null;
  setFocusId: (id: string | null) => void;
  pendingNote: DocPendingNote | null;
  setPendingNote: (note: DocPendingNote | null) => void;
  // The step being dragged by its tag, while a drag is going.
  dragId: string | null;
  setDragId: (id: string | null) => void;
}>({
  editable: false,
  runControlsVisible: false,
  openSettings: () => {},
  openVisual: () => {},
  goTo: () => {},
  selectedBranch: () => undefined,
  previousNoteReader: () => undefined,
  selectBranch: () => {},
  loopOpen: () => true,
  toggleLoop: () => {},
  edit: null,
  destinations: () => [],
  outputUsers: () => [],
  missingReference: () => false,
  focusId: null,
  setFocusId: () => {},
  pendingNote: null,
  setPendingNote: () => {},
  dragId: null,
  setDragId: () => {},
});
