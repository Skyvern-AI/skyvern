import { createContext } from "react";
import type { DocEditApi, DocDestination, DocPlace } from "./docEditApi";

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
  conditionalOpen: (conditionalId: string) => boolean;
  toggleConditional: (conditionalId: string) => void;
  // Null when the doc is read-only (flag on but locked, a run going, or a read-only scope).
  edit: DocEditApi | null;
  destinations: () => DocDestination[];
  // Starts a demonstration at this place; null where one can't start, so the menu leaves the item out.
  // `replacing` is an empty step the recording takes the place of.
  demonstrateAt: (place: DocPlace, replacing?: string) => (() => void) | null;
  outputUsers: (
    id: string,
  ) => Array<{ id: string; label: string; text: string }>;
  missingReference: (reference: string) => boolean;
  // Adds a Text input from the "@" menu and opens it at the top; false when the name can't be used.
  createInput: ((key: string) => boolean) | null;
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
  conditionalOpen: () => true,
  toggleConditional: () => {},
  edit: null,
  destinations: () => [],
  demonstrateAt: () => null,
  outputUsers: () => [],
  missingReference: () => false,
  createInput: null,
  focusId: null,
  setFocusId: () => {},
  pendingNote: null,
  setPendingNote: () => {},
  dragId: null,
  setDragId: () => {},
});
