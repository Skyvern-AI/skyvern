import type { AppNode } from "../nodes";
import type { DocScopeRef } from "./docGraphEdits";
import type { DocPromptUrlOffer } from "./docPromptUrl";
import { createContext } from "react";
import type { DocEditApi, DocDestination, DocPlace } from "./docEditApi";

export type DocPendingNote = {
  ownerNodeId: string;
  placement: "above" | "trailing";
  // Tab put the line in this empty loop or branch. Nothing holds a note there, so the text stays with its owner
  // until "/" picks the step that takes it.
  into?: DocScopeRef;
};

// How an arrival treats the step's Details: "open" shows what it holds (a problem that lives there), "proposal" lets
// the proposal's changes decide again. Without it, the step's Details stay as they were.
export type DocGoToDetails = "open" | "proposal";

export const WorkflowDocActionsContext = createContext<{
  editable: boolean;
  runControlsVisible: boolean;
  // Parameters a login can use as its credential, so it names its credential and not every input it reads.
  loginCredentialKeys: ReadonlySet<string>;
  openSettings: (id: string) => void;
  openVisual: () => void;
  goTo: (id: string, details?: DocGoToDetails) => void;
  selectedBranch: (conditionalId: string) => string | undefined;
  // The step that also reads this step's above note at run time: the one before it in its run sequence.
  previousNoteReader: (nodeId: string) => string | undefined;
  // The URL in the step's prompt, offered for its empty URL field.
  promptUrlOffer: (node: AppNode) => DocPromptUrlOffer | null;
  // Each placed step's number, from docStepNumbers over the whole tree.
  stepNumbers: ReadonlyMap<string, string>;
  selectBranch: (conditionalId: string, branchId: string) => void;
  loopOpen: (loopId: string) => boolean;
  toggleLoop: (loopId: string) => void;
  conditionalOpen: (conditionalId: string) => boolean;
  toggleConditional: (conditionalId: string) => void;
  // Undefined until the user or a navigation opens or closes the step's Details.
  detailsOpen: (id: string) => boolean | undefined;
  setDetailsOpen: (id: string, open: boolean) => void;
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
  loginCredentialKeys: new Set(),
  openSettings: () => {},
  openVisual: () => {},
  goTo: () => {},
  selectedBranch: () => undefined,
  previousNoteReader: () => undefined,
  promptUrlOffer: () => null,
  stepNumbers: new Map(),
  selectBranch: () => {},
  loopOpen: () => true,
  toggleLoop: () => {},
  conditionalOpen: () => true,
  toggleConditional: () => {},
  detailsOpen: () => undefined,
  setDetailsOpen: () => {},
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
