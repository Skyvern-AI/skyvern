// @vitest-environment jsdom

// Mock transitive env-dependent imports so the zustand store can load
// without requiring API base URLs at test time.
vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { Edge } from "@xyflow/react";

import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import type { AppNode } from "../nodes";
import { useWorkflowHistory } from "./useWorkflowHistory";

function makeNode(id: string, label = id, extra: Record<string, unknown> = {}) {
  return {
    id,
    type: "task",
    position: { x: 0, y: 0 },
    data: { label, ...extra },
  } as unknown as AppNode;
}

function resetStore() {
  const s = useWorkflowHasChangesStore.getState();
  s.setHasChanges(false);
  // Drain any leftover internal-update count.
  while (useWorkflowHasChangesStore.getState().internalUpdateCount > 0) {
    s.endInternalUpdate();
  }
}

describe("useWorkflowHistory hook", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    resetStore();
  });

  afterEach(() => {
    vi.useRealTimers();
    resetStore();
  });

  function setup(initialNodes: AppNode[] = [], initialEdges: Edge[] = []) {
    let nodes = initialNodes;
    let edges = initialEdges;

    const setNodes = vi.fn(
      (update: AppNode[] | ((prev: AppNode[]) => AppNode[])) => {
        nodes = typeof update === "function" ? update(nodes) : update;
      },
    );
    const setEdges = vi.fn((update: Edge[] | ((prev: Edge[]) => Edge[])) => {
      edges = typeof update === "function" ? update(edges) : update;
    });

    const hook = renderHook(
      ({ n, e }: { n: AppNode[]; e: Edge[] }) =>
        useWorkflowHistory({ nodes: n, edges: e, setNodes, setEdges }),
      { initialProps: { n: nodes, e: edges } },
    );

    return {
      hook,
      getNodes: () => nodes,
      getEdges: () => edges,
      setNodes,
      setEdges,
      rerender: (n: AppNode[], e: Edge[]) => {
        nodes = n;
        edges = e;
        hook.rerender({ n, e });
      },
    };
  }

  it("captures a baseline and reports canUndo=false initially", () => {
    const { hook } = setup([makeNode("a")]);
    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(hook.result.current.canUndo).toBe(false);
    expect(hook.result.current.canRedo).toBe(false);
  });

  it("capture -> undo -> redo round-trip", () => {
    const n1 = [makeNode("a", "v1")];
    const e1: Edge[] = [];
    const { hook, rerender, setNodes } = setup(n1, e1);

    // Mark dirty so pushes land as user edits (not baseline drift).
    act(() => {
      useWorkflowHasChangesStore.getState().setHasChanges(true);
    });

    // Edit 1: change the label.
    const n2 = [makeNode("a", "v2")];
    act(() => {
      rerender(n2, e1);
    });
    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(hook.result.current.canUndo).toBe(true);

    // Undo back to v1.
    act(() => {
      hook.result.current.undo();
    });
    expect(setNodes).toHaveBeenCalled();
    expect(hook.result.current.canRedo).toBe(true);

    // Redo forward to v2.
    act(() => {
      hook.result.current.redo();
    });
    expect(hook.result.current.canUndo).toBe(true);
    expect(hook.result.current.canRedo).toBe(false);
  });

  it("capture after undo clears the redo stack", () => {
    const n1 = [makeNode("a", "v1")];
    const e1: Edge[] = [];
    const { hook, rerender } = setup(n1, e1);

    act(() => {
      useWorkflowHasChangesStore.getState().setHasChanges(true);
    });

    // v1 -> v2
    act(() => {
      rerender([makeNode("a", "v2")], e1);
    });
    act(() => {
      vi.advanceTimersByTime(500);
    });

    // Undo to v1
    act(() => {
      hook.result.current.undo();
    });
    expect(hook.result.current.canRedo).toBe(true);

    // Simulate React re-rendering with the undo'd state (v1) before
    // the user makes a new edit. In the real app setNodes triggers this
    // automatically; in the test we drive it manually.
    act(() => {
      rerender(n1, e1);
    });

    // New edit v3 (diverge from v2)
    act(() => {
      rerender([makeNode("a", "v3")], e1);
    });
    act(() => {
      vi.advanceTimersByTime(500);
    });

    // Redo should be gone since we diverged.
    expect(hook.result.current.canRedo).toBe(false);
  });

  it("debounce tail does not fire after undo (C2 regression)", () => {
    const n1 = [makeNode("a", "v1")];
    const e1: Edge[] = [];
    const { hook, rerender } = setup(n1, e1);

    act(() => {
      useWorkflowHasChangesStore.getState().setHasChanges(true);
    });

    // Push v2 so we have something to undo to.
    act(() => {
      rerender([makeNode("a", "v2")], e1);
    });
    act(() => {
      vi.advanceTimersByTime(500);
    });

    // Start typing v3 (debounce timer starts but hasn't fired).
    act(() => {
      rerender([makeNode("a", "v3")], e1);
    });
    // Don't advance timers — the debounce is still pending.

    // Undo back to v1 (flushes the pending v3 first, then undoes).
    act(() => {
      hook.result.current.undo();
    });

    // Now advance past the original debounce window. The guarded
    // timer should be a no-op because undo cleared it.
    act(() => {
      vi.advanceTimersByTime(500);
    });

    // We should be able to redo (the undo worked and no stale
    // capture overwrote it).
    expect(hook.result.current.canRedo).toBe(true);
  });

  it("undo is blocked during internal updates", () => {
    const n1 = [makeNode("a", "v1")];
    const e1: Edge[] = [];
    const { hook, rerender } = setup(n1, e1);

    act(() => {
      useWorkflowHasChangesStore.getState().setHasChanges(true);
    });

    act(() => {
      rerender([makeNode("a", "v2")], e1);
    });
    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(hook.result.current.canUndo).toBe(true);

    // Begin an internal update.
    act(() => {
      useWorkflowHasChangesStore.getState().beginInternalUpdate();
    });

    // Undo should be a no-op while internal update is active.
    act(() => {
      hook.result.current.undo();
    });
    // canUndo should still be true (undo didn't fire).
    expect(hook.result.current.canUndo).toBe(true);

    act(() => {
      useWorkflowHasChangesStore.getState().endInternalUpdate();
    });
  });

  it("reorder drop lands as a single undo step (SKY-9064)", () => {
    // Mirror FlowRenderer.onDndDragEnd: the rewire + layout + hasChanges
    // flip happens in one event handler. captureImmediately() is called
    // before the state change so the composite mutation lands as ONE
    // undo entry — no debounced tail, no intermediate drag-frame
    // entries, and the snapshot is the post-drop state.
    const a = makeNode("a");
    const b = makeNode("b");
    const c = makeNode("c");
    const n1 = [a, b, c];
    const e1: Edge[] = [
      { id: "a-b", source: "a", target: "b" } as Edge,
      { id: "b-c", source: "b", target: "c" } as Edge,
    ];
    const { hook, rerender, setNodes, setEdges } = setup(n1, e1);

    // Ensure setup baseline seeded.
    act(() => {
      vi.advanceTimersByTime(500);
    });

    act(() => {
      useWorkflowHasChangesStore.getState().setHasChanges(true);
    });

    // Simulate the drop: request immediate capture, then atomically apply
    // the rewired sibling chain. [A, B, C] -> [A, C, B].
    const n2 = [a, c, b];
    const e2: Edge[] = [
      { id: "a-b", source: "a", target: "c" } as Edge,
      { id: "b-c", source: "c", target: "b" } as Edge,
    ];
    act(() => {
      hook.result.current.captureImmediately();
      rerender(n2, e2);
    });

    // The reorder is undoable immediately - no 300ms debounce wait.
    expect(hook.result.current.canUndo).toBe(true);
    expect(hook.result.current.canRedo).toBe(false);

    // Advance the clock past what would have been the debounce window
    // to confirm no second entry lands from a phantom debounced tail.
    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(hook.result.current.canUndo).toBe(true);

    // Cmd-Z: restores the pre-drag order and edges.
    setNodes.mockClear();
    setEdges.mockClear();
    act(() => {
      hook.result.current.undo();
    });
    const lastNodesCall = setNodes.mock.calls[setNodes.mock.calls.length - 1];
    const lastEdgesCall = setEdges.mock.calls[setEdges.mock.calls.length - 1];
    const undoneNodes = lastNodesCall?.[0] as AppNode[];
    const undoneEdges = lastEdgesCall?.[0] as Edge[];
    expect(undoneNodes.map((n) => n.id)).toEqual(["a", "b", "c"]);
    expect(undoneEdges.map((edge) => `${edge.source}->${edge.target}`)).toEqual(
      ["a->b", "b->c"],
    );
    expect(hook.result.current.canRedo).toBe(true);

    // Drive the hook with the undone state so the subsequent redo sees
    // the restored baseline (in real use setNodes/setEdges feed back
    // through React Flow and the rerender happens automatically).
    act(() => {
      rerender(n1, e1);
    });

    // Cmd-Shift-Z: re-applies the reorder.
    setNodes.mockClear();
    setEdges.mockClear();
    act(() => {
      hook.result.current.redo();
    });
    const redoNodesCall = setNodes.mock.calls[setNodes.mock.calls.length - 1];
    const redoEdgesCall = setEdges.mock.calls[setEdges.mock.calls.length - 1];
    const redoneNodes = redoNodesCall?.[0] as AppNode[];
    const redoneEdges = redoEdgesCall?.[0] as Edge[];
    expect(redoneNodes.map((n) => n.id)).toEqual(["a", "c", "b"]);
    expect(redoneEdges.map((edge) => `${edge.source}->${edge.target}`)).toEqual(
      ["a->c", "c->b"],
    );
    expect(hook.result.current.canUndo).toBe(true);
    expect(hook.result.current.canRedo).toBe(false);
  });

  it("reorder captureImmediately flushes a pending debounced edit (SKY-9064)", () => {
    // Guard rail: a typed edit still in the debounce window must land as
    // its own history entry and not be absorbed into the reorder snapshot.
    // Without this behavior a user who types and immediately reorders
    // would lose an undo step.
    const b = makeNode("b");
    const n1 = [makeNode("a", "v1"), b];
    const e1: Edge[] = [{ id: "a-b", source: "a", target: "b" } as Edge];
    const { hook, rerender } = setup(n1, e1);

    act(() => {
      vi.advanceTimersByTime(500);
    });
    act(() => {
      useWorkflowHasChangesStore.getState().setHasChanges(true);
    });

    // Typed edit v1 -> v2. Do NOT advance timers so the debounce is
    // still pending when the drop fires.
    act(() => {
      rerender([makeNode("a", "v2"), b], e1);
    });

    // Drop immediately — captureImmediately flushes the pending typed
    // edit first, then schedules the reorder snapshot for the next
    // commit.
    const n2 = [b, makeNode("a", "v2")];
    const e2: Edge[] = [{ id: "a-b", source: "b", target: "a" } as Edge];
    act(() => {
      hook.result.current.captureImmediately();
      rerender(n2, e2);
    });

    // Two distinct undoable entries exist: the reorder (present), the
    // typed v2 edit (past[-1]), and the original v1 baseline (past[-2]).
    // First undo returns to the typed pre-drop state.
    act(() => {
      hook.result.current.undo();
    });
    expect(hook.result.current.canUndo).toBe(true);
    expect(hook.result.current.canRedo).toBe(true);

    // Second undo returns to v1.
    act(() => {
      rerender([makeNode("a", "v2"), b], e1);
    });
    act(() => {
      hook.result.current.undo();
    });
    expect(hook.result.current.canUndo).toBe(false);
    expect(hook.result.current.canRedo).toBe(true);
  });

  it("captureImmediately does not capture intermediate drag frames (SKY-9064)", () => {
    // Composite AC: rerenders with the same structural content while a
    // drag is in progress (React Flow may hand us fresh arrays with the
    // `dragging` runtime flag toggled) must not produce history entries.
    // The snapshot equality check strips runtime fields so only the
    // final drop — marked by captureImmediately — becomes an entry.
    const a = makeNode("a");
    const b = makeNode("b");
    const n1 = [a, b];
    const e1: Edge[] = [{ id: "a-b", source: "a", target: "b" } as Edge];
    const { hook, rerender } = setup(n1, e1);

    act(() => {
      vi.advanceTimersByTime(500);
    });
    act(() => {
      useWorkflowHasChangesStore.getState().setHasChanges(true);
    });

    // Simulate multiple mid-drag frames: same ids/edges, dragging flag
    // toggled on the active node. Each frame is a distinct array
    // reference (React Flow reallocates on every change) but cloneNode
    // strips `dragging` so snapshotsEqual reports no change.
    const dragging = (node: AppNode) =>
      ({ ...node, dragging: true }) as unknown as AppNode;
    for (let frame = 0; frame < 5; frame++) {
      act(() => {
        rerender([dragging(a), b], e1);
      });
    }
    act(() => {
      vi.advanceTimersByTime(500);
    });
    // No drop has fired - the hook should still be at baseline.
    expect(hook.result.current.canUndo).toBe(false);

    // Now the drop: one atomic reorder, one undo step.
    act(() => {
      hook.result.current.captureImmediately();
      rerender([b, a], [{ id: "a-b", source: "b", target: "a" } as Edge]);
    });
    expect(hook.result.current.canUndo).toBe(true);

    // A second undo would bottom out if any intermediate drag frame had
    // been captured. canUndo flips false after the single step.
    act(() => {
      hook.result.current.undo();
    });
    expect(hook.result.current.canUndo).toBe(false);
  });

  it("undo is blocked while a node is mid-drag (R5)", () => {
    const draggingNode = {
      ...makeNode("a", "v1"),
      dragging: true,
    } as unknown as AppNode;
    const e1: Edge[] = [];
    const { hook, rerender } = setup([makeNode("a", "v1")], e1);

    act(() => {
      useWorkflowHasChangesStore.getState().setHasChanges(true);
    });

    act(() => {
      rerender([makeNode("a", "v2")], e1);
    });
    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(hook.result.current.canUndo).toBe(true);

    // Simulate mid-drag: rerender with dragging=true on the node.
    act(() => {
      rerender([draggingNode], e1);
    });

    // Undo should bail because a node is dragging.
    act(() => {
      hook.result.current.undo();
    });
    expect(hook.result.current.canUndo).toBe(true);
  });

  it("historyApplyTrigger bumps on every undo/redo (SKY-9051)", () => {
    // FlowRenderer uses this counter to fire a doLayout pass after the
    // restored nodes commit, so undoing across a loop/conditional expand
    // toggle doesn't leave children positioned against the prior
    // container's layout.
    const n1 = [makeNode("a", "v1")];
    const e1: Edge[] = [];
    const { hook, rerender } = setup(n1, e1);
    act(() => {
      useWorkflowHasChangesStore.getState().setHasChanges(true);
    });

    expect(hook.result.current.historyApplyTrigger).toBe(0);

    // Push v2 so undo has a target.
    act(() => {
      rerender([makeNode("a", "v2")], e1);
    });
    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(hook.result.current.historyApplyTrigger).toBe(0);

    act(() => {
      hook.result.current.undo();
    });
    expect(hook.result.current.historyApplyTrigger).toBe(1);

    act(() => {
      rerender(n1, e1);
    });
    act(() => {
      hook.result.current.redo();
    });
    expect(hook.result.current.historyApplyTrigger).toBe(2);
  });
});
