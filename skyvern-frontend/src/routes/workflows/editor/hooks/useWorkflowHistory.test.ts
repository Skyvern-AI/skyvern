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
});
