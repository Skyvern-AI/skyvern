import type { Edge } from "@xyflow/react";
import type { AppNode } from "../nodes";

// Snapshot of the workflow editor canvas at a point in time. We store the
// full nodes/edges arrays (deep-cloned at capture time) so that rolling back
// is just a matter of calling setNodes/setEdges with the previous snapshot.
export type WorkflowSnapshot = {
  nodes: AppNode[];
  edges: Edge[];
};

export type WorkflowHistoryState = {
  past: WorkflowSnapshot[];
  present: WorkflowSnapshot | null;
  future: WorkflowSnapshot[];
};

// Hard cap on how many undo entries we keep. When a new entry would
// push the stack past this limit, the oldest entry is dropped - so
// from the user's perspective Undo walks back at most this many steps
// and then bottoms out silently on whatever baseline is left. 50
// mirrors typical editor behaviour (VSCode-ish) and bounds memory
// growth for large workflows.
export const MAX_HISTORY_ENTRIES = 50;

export function createInitialHistoryState(): WorkflowHistoryState {
  return { past: [], present: null, future: [] };
}

// Deep clone a snapshot. Node/edge data objects are mutable in the editor
// (see updateNodeData() callers in the node components) so a shallow copy
// would let later edits bleed into history entries.
export function cloneSnapshot(
  nodes: readonly AppNode[],
  edges: readonly Edge[],
): WorkflowSnapshot {
  return {
    nodes: nodes.map((node) => cloneNode(node)),
    edges: edges.map((edge) => cloneEdge(edge)),
  };
}

// Fields React Flow maintains at runtime from the rendered DOM. They must
// NOT be part of a history snapshot: reapplying a stale `measured` / width
// / height / selected / dragging causes a flicker because RF thinks the
// node is already measured at outdated dimensions before re-measuring.
const REACT_FLOW_RUNTIME_FIELDS = [
  "measured",
  "width",
  "height",
  "selected",
  "dragging",
  "positionAbsolute",
] as const;

function cloneNode(node: AppNode): AppNode {
  // Strip runtime fields first, then deep-clone the whole filtered
  // object. This protects against in-place mutation of any field
  // (data, style, className, position, etc.) leaking into snapshots
  // already pushed onto history.
  const filtered: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(node)) {
    if ((REACT_FLOW_RUNTIME_FIELDS as readonly string[]).includes(key)) {
      continue;
    }
    filtered[key] = value;
  }
  return deepClone(filtered) as AppNode;
}

// React Flow sets `selected` on edges from user interaction; treat it the
// same as its node-level runtime counterparts so undo/redo doesn't briefly
// re-highlight an edge before RF clears it. Extend this list if additional
// edge flicker is observed on snapshot restore, mirroring the node-side
// REACT_FLOW_RUNTIME_FIELDS pattern above.
const REACT_FLOW_EDGE_RUNTIME_FIELDS = ["selected"] as const;

function cloneEdge(edge: Edge): Edge {
  // Same pattern as cloneNode: strip runtime fields, then deep-clone
  // the whole object so mutations to edge.style / edge.markerEnd /
  // edge.label etc. can't bleed into snapshots on the history stack.
  const filtered: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(edge)) {
    if ((REACT_FLOW_EDGE_RUNTIME_FIELDS as readonly string[]).includes(key)) {
      continue;
    }
    filtered[key] = value;
  }
  return deepClone(filtered) as Edge;
}

function deepClone<T>(value: T): T {
  // structuredClone is universally available in our target environments
  // (all supported browsers and Node >= 17). Snapshot data is plain
  // JSON-ish - nodes/edges and their `data` objects - so the clone
  // never encounters functions or other unstructured values.
  return structuredClone(value);
}

// Order-independent structural equality. We don't use JSON.stringify because
// React Flow occasionally reorders keys between renders (e.g. when `measured`
// is added), which would falsely report two semantically-identical snapshots
// as different and generate spurious history entries.
function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (
    a === null ||
    b === null ||
    typeof a !== "object" ||
    typeof b !== "object"
  ) {
    return false;
  }
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a)) {
    const bArr = b as unknown[];
    if (a.length !== bArr.length) return false;
    for (let i = 0; i < a.length; i++) {
      if (!deepEqual(a[i], bArr[i])) return false;
    }
    return true;
  }
  const aRec = a as Record<string, unknown>;
  const bRec = b as Record<string, unknown>;
  const aKeys = Object.keys(aRec);
  const bKeys = Object.keys(bRec);
  if (aKeys.length !== bKeys.length) return false;
  for (const key of aKeys) {
    if (!Object.prototype.hasOwnProperty.call(bRec, key)) return false;
    if (!deepEqual(aRec[key], bRec[key])) return false;
  }
  return true;
}

// Compare two node arrays by id, independent of array order. React Flow
// occasionally reorders nodes between renders (reconciliation passes,
// copy-paste insertions, etc.), which would make a positional compare
// flag a semantically identical state as different.
function nodesEqualById(a: AppNode[], b: AppNode[]): boolean {
  if (a.length !== b.length) return false;
  const bMap = new Map<string, AppNode>();
  for (const node of b) bMap.set(node.id, node);
  for (const an of a) {
    const bn = bMap.get(an.id);
    if (!bn) return false;
    if (!deepEqual(an, bn)) return false;
  }
  return true;
}

// Compare two edge arrays by id, independent of array order. Same
// rationale as nodesEqualById.
function edgesEqualById(a: Edge[], b: Edge[]): boolean {
  if (a.length !== b.length) return false;
  const bMap = new Map<string, Edge>();
  for (const edge of b) bMap.set(edge.id, edge);
  for (const ae of a) {
    const be = bMap.get(ae.id);
    if (!be) return false;
    if (!deepEqual(ae, be)) return false;
  }
  return true;
}

/** Both snapshots must be pre-cloned (via {@link cloneSnapshot}) before
 *  comparison; this function does not strip React Flow runtime fields. */
export function snapshotsEqual(
  a: WorkflowSnapshot,
  b: WorkflowSnapshot,
): boolean {
  if (a === b) return true;
  if (a.nodes.length !== b.nodes.length) return false;
  if (a.edges.length !== b.edges.length) return false;
  return nodesEqualById(a.nodes, b.nodes) && edgesEqualById(a.edges, b.edges);
}

// Push a new snapshot as the present state, moving the previous present into
// the past stack and clearing any redo stack. Returns the original state
// unchanged when the snapshot is identical to the current present (no-op).
export function pushSnapshot(
  state: WorkflowHistoryState,
  snapshot: WorkflowSnapshot,
): WorkflowHistoryState {
  if (state.present === null) {
    return { past: [], present: snapshot, future: [] };
  }
  if (snapshotsEqual(state.present, snapshot)) {
    return state;
  }
  const nextPast = [...state.past, state.present];
  if (nextPast.length > MAX_HISTORY_ENTRIES) {
    // Drop the oldest entry so the stack stays bounded.
    nextPast.shift();
  }
  return { past: nextPast, present: snapshot, future: [] };
}

// Replace the present without growing the past stack. Used when we want the
// history baseline to catch up after an internal (non-user) update like a
// branch switch, or during the mount-time settle window, without creating
// an undo step the user didn't ask for.
//
// The redo (`future`) stack is cleared because any entries it contained are
// snapshots from the pre-replace timeline and would be invalid to jump back
// to. `past` is preserved so ordinary layout-only updates don't nuke the
// user's edit history.
export function replacePresent(
  state: WorkflowHistoryState,
  snapshot: WorkflowSnapshot,
): WorkflowHistoryState {
  if (
    state.present !== null &&
    state.future.length === 0 &&
    snapshotsEqual(state.present, snapshot)
  ) {
    return state;
  }
  return { past: state.past, present: snapshot, future: [] };
}

export function canUndo(state: WorkflowHistoryState): boolean {
  return state.past.length > 0 && state.present !== null;
}

export function canRedo(state: WorkflowHistoryState): boolean {
  return state.future.length > 0 && state.present !== null;
}

// Walk one step back. Returns the new state and the snapshot to apply to the
// editor, or null when there is nothing to undo.
export function undo(state: WorkflowHistoryState): {
  state: WorkflowHistoryState;
  applied: WorkflowSnapshot;
} | null {
  if (!canUndo(state)) return null;
  const nextPast = state.past.slice(0, -1);
  const applied = state.past[state.past.length - 1]!;
  const nextFuture = [state.present!, ...state.future];
  return {
    state: { past: nextPast, present: applied, future: nextFuture },
    applied,
  };
}

// Walk one step forward. Returns the new state and the snapshot to apply, or
// null when there is nothing to redo.
export function redo(state: WorkflowHistoryState): {
  state: WorkflowHistoryState;
  applied: WorkflowSnapshot;
} | null {
  if (!canRedo(state)) return null;
  const applied = state.future[0]!;
  const nextFuture = state.future.slice(1);
  const nextPast = [...state.past, state.present!];
  return {
    state: { past: nextPast, present: applied, future: nextFuture },
    applied,
  };
}
