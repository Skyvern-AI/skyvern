import { describe, expect, it } from "vitest";
import type { Edge } from "@xyflow/react";

import type { AppNode } from "../nodes";
import {
  canRedo,
  canUndo,
  cloneSnapshot,
  createInitialHistoryState,
  MAX_HISTORY_ENTRIES,
  pushSnapshot,
  redo,
  replacePresent,
  snapshotsEqual,
  undo,
  type WorkflowHistoryState,
  type WorkflowSnapshot,
} from "./workflowHistoryState";

// Shape of a node we build for tests. The production type is a large
// discriminated union; casting keeps the tests focused on history logic
// without dragging in every node type.
function makeNode(id: string, label = id, extra: Record<string, unknown> = {}) {
  return {
    id,
    type: "task",
    position: { x: 0, y: 0 },
    data: { label, ...extra },
  } as unknown as AppNode;
}

function makeEdge(id: string, source: string, target: string): Edge {
  return { id, source, target };
}

function snapshot(nodes: AppNode[], edges: Edge[] = []): WorkflowSnapshot {
  return { nodes, edges };
}

function snapshotWithLabel(id: string, label: string): WorkflowSnapshot {
  return snapshot([makeNode(id, label)]);
}

describe("workflowHistoryState / cloneSnapshot", () => {
  it("returns a deep copy so mutations don't leak into history", () => {
    const node = makeNode("a", "first");
    const edge = makeEdge("e1", "a", "b");
    const clone = cloneSnapshot([node], [edge]);

    // Mutate the source and ensure the clone is unaffected.
    (node.data as { label: string }).label = "mutated";
    edge.source = "z";

    expect((clone.nodes[0]!.data as { label: string }).label).toBe("first");
    expect(clone.edges[0]!.source).toBe("a");
  });

  it("strips React Flow runtime `selected` from cloned edges", () => {
    const edge = {
      id: "e1",
      source: "a",
      target: "b",
      selected: true,
    } as unknown as Edge;
    const clone = cloneSnapshot([], [edge]);
    expect(clone.edges[0] as Record<string, unknown>).not.toHaveProperty(
      "selected",
    );
    expect(clone.edges[0]!.id).toBe("e1");
  });

  it("strips React Flow runtime-maintained fields from cloned nodes", () => {
    // React Flow mutates these on the fly from the rendered DOM; keeping
    // them in a snapshot would reapply stale dimensions on undo/redo.
    const node = {
      id: "a",
      type: "task",
      position: { x: 0, y: 0 },
      data: { label: "a" },
      measured: { width: 200, height: 80 },
      width: 200,
      height: 80,
      selected: true,
      dragging: true,
      positionAbsolute: { x: 100, y: 100 },
    } as unknown as AppNode;

    const clone = cloneSnapshot([node], []);
    const cloned = clone.nodes[0] as Record<string, unknown>;

    expect(cloned).not.toHaveProperty("measured");
    expect(cloned).not.toHaveProperty("width");
    expect(cloned).not.toHaveProperty("height");
    expect(cloned).not.toHaveProperty("selected");
    expect(cloned).not.toHaveProperty("dragging");
    expect(cloned).not.toHaveProperty("positionAbsolute");
    // Core fields must still round-trip.
    expect(cloned.id).toBe("a");
    expect(cloned.type).toBe("task");
  });

  it("clones nested data objects", () => {
    const node = makeNode("a", "a", {
      parameters: { inputs: ["x"] },
    });
    const clone = cloneSnapshot([node], []);
    const clonedData = clone.nodes[0]!.data as unknown as {
      parameters: { inputs: string[] };
    };
    clonedData.parameters.inputs.push("y");

    const sourceData = node.data as unknown as {
      parameters: { inputs: string[] };
    };
    expect(sourceData.parameters.inputs).toEqual(["x"]);
  });
});

describe("workflowHistoryState / snapshotsEqual", () => {
  it("returns true for structurally identical snapshots", () => {
    const a = snapshot([makeNode("n1")], [makeEdge("e1", "n1", "n2")]);
    const b = snapshot([makeNode("n1")], [makeEdge("e1", "n1", "n2")]);
    expect(snapshotsEqual(a, b)).toBe(true);
  });

  it("returns false when node counts differ", () => {
    const a = snapshot([makeNode("n1")]);
    const b = snapshot([makeNode("n1"), makeNode("n2")]);
    expect(snapshotsEqual(a, b)).toBe(false);
  });

  it("returns false when nested data differs", () => {
    expect(
      snapshotsEqual(
        snapshotWithLabel("n1", "old"),
        snapshotWithLabel("n1", "new"),
      ),
    ).toBe(false);
  });

  it("is independent of node array ordering", () => {
    // React Flow may reorder nodes between renders (reconciliation,
    // copy-paste, etc.). The history comparator must not treat that as
    // a structural change.
    const a = snapshot([makeNode("n1", "v1"), makeNode("n2", "v2")]);
    const b = snapshot([makeNode("n2", "v2"), makeNode("n1", "v1")]);
    expect(snapshotsEqual(a, b)).toBe(true);
  });

  it("is independent of edge array ordering", () => {
    const nodes = [makeNode("n1"), makeNode("n2"), makeNode("n3")];
    const a = snapshot(nodes, [
      makeEdge("e1", "n1", "n2"),
      makeEdge("e2", "n2", "n3"),
    ]);
    const b = snapshot(nodes, [
      makeEdge("e2", "n2", "n3"),
      makeEdge("e1", "n1", "n2"),
    ]);
    expect(snapshotsEqual(a, b)).toBe(true);
  });

  it("is key-order independent", () => {
    // React Flow sometimes reshuffles key order between renders (e.g. when
    // `measured` shows up); a JSON.stringify comparison would flag these
    // as different and create spurious history entries.
    const a = snapshot([
      {
        id: "n1",
        type: "task",
        position: { x: 0, y: 0 },
        data: { label: "same" },
      } as unknown as AppNode,
    ]);
    const b = snapshot([
      {
        data: { label: "same" },
        position: { y: 0, x: 0 },
        type: "task",
        id: "n1",
      } as unknown as AppNode,
    ]);
    expect(snapshotsEqual(a, b)).toBe(true);
  });
});

describe("workflowHistoryState / pushSnapshot", () => {
  it("seeds the present on the first push", () => {
    const state = pushSnapshot(
      createInitialHistoryState(),
      snapshotWithLabel("n1", "first"),
    );
    expect(state.present).not.toBeNull();
    expect(state.past).toHaveLength(0);
    expect(state.future).toHaveLength(0);
  });

  it("moves the old present into past and clears future on a new push", () => {
    let state = pushSnapshot(
      createInitialHistoryState(),
      snapshotWithLabel("n1", "v1"),
    );
    state = pushSnapshot(state, snapshotWithLabel("n1", "v2"));
    // Simulate an outstanding redo stack that should be cleared by a new edit.
    state = { ...state, future: [snapshotWithLabel("n1", "stale")] };

    state = pushSnapshot(state, snapshotWithLabel("n1", "v3"));

    expect(state.past).toHaveLength(2);
    expect(state.future).toHaveLength(0);
    expect((state.present!.nodes[0]!.data as { label: string }).label).toBe(
      "v3",
    );
  });

  it("is a no-op when the snapshot equals the current present", () => {
    const seeded = pushSnapshot(
      createInitialHistoryState(),
      snapshotWithLabel("n1", "same"),
    );
    const after = pushSnapshot(seeded, snapshotWithLabel("n1", "same"));
    expect(after).toBe(seeded);
  });

  it("caps the past stack at MAX_HISTORY_ENTRIES", () => {
    let state: WorkflowHistoryState = createInitialHistoryState();
    // Seed once so the first real push lands in `past`.
    state = pushSnapshot(state, snapshotWithLabel("n1", "v0"));
    for (let i = 1; i <= MAX_HISTORY_ENTRIES + 5; i++) {
      state = pushSnapshot(state, snapshotWithLabel("n1", `v${i}`));
    }
    expect(state.past.length).toBe(MAX_HISTORY_ENTRIES);
    // Oldest surviving entry should be v5 - we overflowed by 5.
    const oldest = state.past[0]!;
    expect((oldest.nodes[0]!.data as { label: string }).label).toBe("v5");
  });
});

describe("workflowHistoryState / undo", () => {
  it("returns null when there is nothing to undo", () => {
    expect(undo(createInitialHistoryState())).toBeNull();
    const seeded = pushSnapshot(
      createInitialHistoryState(),
      snapshotWithLabel("n1", "only"),
    );
    expect(undo(seeded)).toBeNull();
  });

  it("restores the previous snapshot and pushes present into future", () => {
    let state: WorkflowHistoryState = createInitialHistoryState();
    state = pushSnapshot(state, snapshotWithLabel("n1", "v1"));
    state = pushSnapshot(state, snapshotWithLabel("n1", "v2"));
    state = pushSnapshot(state, snapshotWithLabel("n1", "v3"));

    const result = undo(state)!;
    expect(result).not.toBeNull();
    expect((result.applied.nodes[0]!.data as { label: string }).label).toBe(
      "v2",
    );
    expect(result.state.past).toHaveLength(1);
    expect(result.state.future).toHaveLength(1);
    expect(
      (result.state.future[0]!.nodes[0]!.data as { label: string }).label,
    ).toBe("v3");
  });
});

describe("workflowHistoryState / redo", () => {
  it("returns null when the future stack is empty", () => {
    const state = pushSnapshot(
      createInitialHistoryState(),
      snapshotWithLabel("n1", "v1"),
    );
    expect(redo(state)).toBeNull();
  });

  it("moves the next future snapshot back into present", () => {
    let state: WorkflowHistoryState = createInitialHistoryState();
    state = pushSnapshot(state, snapshotWithLabel("n1", "v1"));
    state = pushSnapshot(state, snapshotWithLabel("n1", "v2"));

    const undone = undo(state)!;
    const redone = redo(undone.state)!;

    expect((redone.applied.nodes[0]!.data as { label: string }).label).toBe(
      "v2",
    );
    expect(redone.state.future).toHaveLength(0);
    expect(redone.state.past).toHaveLength(1);
  });

  it("is cleared when a new push happens after an undo", () => {
    let state: WorkflowHistoryState = createInitialHistoryState();
    state = pushSnapshot(state, snapshotWithLabel("n1", "v1"));
    state = pushSnapshot(state, snapshotWithLabel("n1", "v2"));
    state = pushSnapshot(state, snapshotWithLabel("n1", "v3"));

    state = undo(state)!.state;
    expect(state.future).toHaveLength(1);

    state = pushSnapshot(state, snapshotWithLabel("n1", "v2b"));
    expect(state.future).toHaveLength(0);
    expect(redo(state)).toBeNull();
  });
});

describe("workflowHistoryState / replacePresent", () => {
  it("updates the baseline without touching the past stack", () => {
    let state: WorkflowHistoryState = createInitialHistoryState();
    state = pushSnapshot(state, snapshotWithLabel("n1", "v1"));
    state = pushSnapshot(state, snapshotWithLabel("n1", "v2"));
    const pastBefore = state.past;

    const replaced = replacePresent(state, snapshotWithLabel("n1", "v2-int"));
    expect(replaced.past).toBe(pastBefore);
    expect((replaced.present!.nodes[0]!.data as { label: string }).label).toBe(
      "v2-int",
    );
  });

  it("clears the redo stack so stale snapshots aren't reachable", () => {
    // User edits v1→v2→v3, undoes twice, then an internal update (branch
    // switch, layout pass) replaces the present. The existing redo stack
    // is from the pre-replace timeline and must not be reachable.
    let state: WorkflowHistoryState = createInitialHistoryState();
    state = pushSnapshot(state, snapshotWithLabel("n1", "v1"));
    state = pushSnapshot(state, snapshotWithLabel("n1", "v2"));
    state = pushSnapshot(state, snapshotWithLabel("n1", "v3"));
    state = undo(state)!.state;
    state = undo(state)!.state;
    expect(state.future).toHaveLength(2);

    const replaced = replacePresent(state, snapshotWithLabel("n1", "branchX"));
    expect(replaced.future).toHaveLength(0);
    expect(redo(replaced)).toBeNull();
  });

  it("returns the original state when the snapshot equals the present and future is empty", () => {
    const seeded = pushSnapshot(
      createInitialHistoryState(),
      snapshotWithLabel("n1", "same"),
    );
    const replaced = replacePresent(seeded, snapshotWithLabel("n1", "same"));
    expect(replaced).toBe(seeded);
  });

  it("still clears future even when the snapshot matches the present", () => {
    // If there's a stale redo stack sitting around, we must not return
    // the unchanged state - the redo entries are still invalid.
    let state: WorkflowHistoryState = createInitialHistoryState();
    state = pushSnapshot(state, snapshotWithLabel("n1", "v1"));
    state = pushSnapshot(state, snapshotWithLabel("n1", "v2"));
    state = undo(state)!.state;
    expect(state.future).toHaveLength(1);

    const replaced = replacePresent(state, snapshotWithLabel("n1", "v1"));
    expect(replaced.future).toHaveLength(0);
  });
});

describe("workflowHistoryState / canUndo & canRedo flags", () => {
  it("tracks availability of undo/redo through a full edit cycle", () => {
    let state: WorkflowHistoryState = createInitialHistoryState();
    expect(canUndo(state)).toBe(false);
    expect(canRedo(state)).toBe(false);

    state = pushSnapshot(state, snapshotWithLabel("n1", "v1"));
    expect(canUndo(state)).toBe(false);

    state = pushSnapshot(state, snapshotWithLabel("n1", "v2"));
    expect(canUndo(state)).toBe(true);
    expect(canRedo(state)).toBe(false);

    state = undo(state)!.state;
    expect(canUndo(state)).toBe(false);
    expect(canRedo(state)).toBe(true);

    state = redo(state)!.state;
    expect(canRedo(state)).toBe(false);
    expect(canUndo(state)).toBe(true);
  });
});
