import type { Edge } from "@xyflow/react";
import { describe, expect, test } from "vitest";

import { classifyBlockDrop, rewireBlockDropInScope } from "./rewire";
import { TOP_LEVEL_SCOPE, type SortableScopeNode } from "./scope";

function start(id: string, parentId?: string): SortableScopeNode {
  return parentId === undefined
    ? { id, type: "start" }
    : { id, type: "start", parentId };
}

function block(id: string, parentId?: string): SortableScopeNode {
  return parentId === undefined
    ? { id, type: "task" }
    : { id, type: "task", parentId };
}

function loop(id: string, parentId?: string): SortableScopeNode {
  return parentId === undefined
    ? { id, type: "loop" }
    : { id, type: "loop", parentId };
}

function conditional(id: string, parentId?: string): SortableScopeNode {
  return parentId === undefined
    ? { id, type: "conditional" }
    : { id, type: "conditional", parentId };
}

function adder(id: string, parentId?: string): SortableScopeNode {
  return parentId === undefined
    ? { id, type: "nodeAdder" }
    : { id, type: "nodeAdder", parentId };
}

function edge(
  id: string,
  source: string,
  target: string,
  type: "edgeWithAddButton" | "default" = "edgeWithAddButton",
): Edge {
  return { id, source, target, type } as Edge;
}

function branchEdge(
  id: string,
  source: string,
  target: string,
  conditionalNodeId: string,
  conditionalBranchId: string,
  type: "edgeWithAddButton" | "default" = "edgeWithAddButton",
): Edge {
  return {
    id,
    source,
    target,
    type,
    data: { conditionalNodeId, conditionalBranchId },
  } as Edge;
}

/**
 * Derive the sibling chain order from an edge list by walking from `startId`
 * until we hit `adderId`. Lets tests assert on the resulting chain without
 * depending on insertion order in the edges array.
 */
function walkChain(
  edges: Array<Edge>,
  startId: string,
  adderId: string,
): Array<string> {
  const order: Array<string> = [];
  const visited = new Set<string>();
  let next = edges.find((e) => e.source === startId)?.target;
  while (next && next !== adderId && !visited.has(next)) {
    visited.add(next);
    order.push(next);
    next = edges.find((e) => e.source === next)?.target;
  }
  return order;
}

function chainEdgeType(
  edges: Array<Edge>,
  source: string,
  target: string,
): string | undefined {
  return edges.find((e) => e.source === source && e.target === target)?.type;
}

const nodes: Array<SortableScopeNode> = [
  start("start"),
  block("a"),
  block("b"),
  block("c"),
  block("d"),
  adder("adder"),
];

const baseEdges: Array<Edge> = [
  edge("e0", "start", "a"),
  edge("e1", "a", "b"),
  edge("e2", "b", "c"),
  edge("e3", "c", "d"),
  edge("e4", "d", "adder", "default"),
];

describe("rewireBlockDropInScope", () => {
  test("move middle block to head produces correct chain", () => {
    const result = rewireBlockDropInScope({
      nodes,
      edges: baseEdges,
      scope: TOP_LEVEL_SCOPE,
      activeId: "c",
      overId: "a",
    });
    expect(result).not.toBeNull();
    expect(result!.newOrder).toEqual(["c", "a", "b", "d"]);
    expect(walkChain(result!.edges, "start", "adder")).toEqual([
      "c",
      "a",
      "b",
      "d",
    ]);
  });

  test("backward adjacent swap (b dropped onto a) produces correct chain", () => {
    // Covers the oldIndex=1 -> newIndex=0 case symmetric to the forward
    // adjacent swap — arrayMove handles it correctly but the suite otherwise
    // only covers forward / long-distance moves.
    const result = rewireBlockDropInScope({
      nodes,
      edges: baseEdges,
      scope: TOP_LEVEL_SCOPE,
      activeId: "b",
      overId: "a",
    });
    expect(result).not.toBeNull();
    expect(result!.newOrder).toEqual(["b", "a", "c", "d"]);
    expect(walkChain(result!.edges, "start", "adder")).toEqual([
      "b",
      "a",
      "c",
      "d",
    ]);
  });

  test("move middle block to tail produces correct chain", () => {
    const result = rewireBlockDropInScope({
      nodes,
      edges: baseEdges,
      scope: TOP_LEVEL_SCOPE,
      activeId: "b",
      overId: "d",
    });
    expect(result).not.toBeNull();
    expect(result!.newOrder).toEqual(["a", "c", "d", "b"]);
    expect(walkChain(result!.edges, "start", "adder")).toEqual([
      "a",
      "c",
      "d",
      "b",
    ]);
  });

  test("move head block to middle produces correct chain", () => {
    const result = rewireBlockDropInScope({
      nodes,
      edges: baseEdges,
      scope: TOP_LEVEL_SCOPE,
      activeId: "a",
      overId: "c",
    });
    expect(result).not.toBeNull();
    expect(result!.newOrder).toEqual(["b", "c", "a", "d"]);
    expect(walkChain(result!.edges, "start", "adder")).toEqual([
      "b",
      "c",
      "a",
      "d",
    ]);
  });

  test("move tail block to head produces correct chain", () => {
    const result = rewireBlockDropInScope({
      nodes,
      edges: baseEdges,
      scope: TOP_LEVEL_SCOPE,
      activeId: "d",
      overId: "a",
    });
    expect(result).not.toBeNull();
    expect(result!.newOrder).toEqual(["d", "a", "b", "c"]);
    expect(walkChain(result!.edges, "start", "adder")).toEqual([
      "d",
      "a",
      "b",
      "c",
    ]);
  });

  test("edge types: last-to-adder stays default, block-to-block stays edgeWithAddButton", () => {
    const result = rewireBlockDropInScope({
      nodes,
      edges: baseEdges,
      scope: TOP_LEVEL_SCOPE,
      activeId: "a",
      overId: "d",
    });
    expect(result).not.toBeNull();
    expect(result!.newOrder).toEqual(["b", "c", "d", "a"]);
    expect(chainEdgeType(result!.edges, "start", "b")).toBe(
      "edgeWithAddButton",
    );
    expect(chainEdgeType(result!.edges, "b", "c")).toBe("edgeWithAddButton");
    expect(chainEdgeType(result!.edges, "c", "d")).toBe("edgeWithAddButton");
    expect(chainEdgeType(result!.edges, "d", "a")).toBe("edgeWithAddButton");
    // the sibling that now terminates the chain must keep the default type
    // so the NodeAdder connector stays plus-button-free
    expect(chainEdgeType(result!.edges, "a", "adder")).toBe("default");
  });

  test("edge count is preserved across a rewire", () => {
    const result = rewireBlockDropInScope({
      nodes,
      edges: baseEdges,
      scope: TOP_LEVEL_SCOPE,
      activeId: "b",
      overId: "d",
    });
    expect(result).not.toBeNull();
    expect(result!.edges.length).toBe(baseEdges.length);
  });

  test("existing edge ids are reused at the same chain position so react-flow keys survive", () => {
    const result = rewireBlockDropInScope({
      nodes,
      edges: baseEdges,
      scope: TOP_LEVEL_SCOPE,
      activeId: "c",
      overId: "a",
    });
    expect(result).not.toBeNull();
    const newIds = result!.edges.map((e) => e.id).sort();
    const oldIds = baseEdges.map((e) => e.id).sort();
    expect(newIds).toEqual(oldIds);
  });

  test("drop on self is a no-op", () => {
    const result = rewireBlockDropInScope({
      nodes,
      edges: baseEdges,
      scope: TOP_LEVEL_SCOPE,
      activeId: "b",
      overId: "b",
    });
    expect(result).toBeNull();
  });

  test("drop with null over (outside any sortable slot) is a no-op", () => {
    const result = rewireBlockDropInScope({
      nodes,
      edges: baseEdges,
      scope: TOP_LEVEL_SCOPE,
      activeId: "b",
      overId: null,
    });
    expect(result).toBeNull();
  });

  test("drop onto a non-sibling id is a no-op", () => {
    const result = rewireBlockDropInScope({
      nodes,
      edges: baseEdges,
      scope: TOP_LEVEL_SCOPE,
      activeId: "b",
      overId: "adder",
    });
    expect(result).toBeNull();
  });

  test("unknown active id is a no-op", () => {
    const result = rewireBlockDropInScope({
      nodes,
      edges: baseEdges,
      scope: TOP_LEVEL_SCOPE,
      activeId: "unknown",
      overId: "a",
    });
    expect(result).toBeNull();
  });

  test("empty scope chain is a no-op", () => {
    const emptyNodes: Array<SortableScopeNode> = [
      start("start"),
      adder("adder"),
    ];
    const emptyEdges: Array<Edge> = [edge("e0", "start", "adder", "default")];
    const result = rewireBlockDropInScope({
      nodes: emptyNodes,
      edges: emptyEdges,
      scope: TOP_LEVEL_SCOPE,
      activeId: "x",
      overId: "y",
    });
    expect(result).toBeNull();
  });

  test("single-sibling scope is always a no-op", () => {
    const oneNode: Array<SortableScopeNode> = [
      start("start"),
      block("only"),
      adder("adder"),
    ];
    const oneEdgeSet: Array<Edge> = [
      edge("e0", "start", "only"),
      edge("e1", "only", "adder", "default"),
    ];
    const result = rewireBlockDropInScope({
      nodes: oneNode,
      edges: oneEdgeSet,
      scope: TOP_LEVEL_SCOPE,
      activeId: "only",
      overId: "only",
    });
    expect(result).toBeNull();
  });

  test("non-chain edges are preserved untouched", () => {
    // Add a dangling edge pointing at an unrelated node (e.g. a conditional
    // internal branch edge) and verify it survives the rewire intact.
    const extraEdges: Array<Edge> = [
      ...baseEdges,
      edge("e-extra", "a", "some-inner-node", "default"),
    ];
    const result = rewireBlockDropInScope({
      nodes,
      edges: extraEdges,
      scope: TOP_LEVEL_SCOPE,
      activeId: "c",
      overId: "a",
    });
    expect(result).not.toBeNull();
    const extra = result!.edges.find((e) => e.id === "e-extra");
    expect(extra).toBeDefined();
    expect(extra!.source).toBe("a");
    expect(extra!.target).toBe("some-inner-node");
  });

  describe("loop-container scope (3-block fixture, SKY-9057)", () => {
    // Mirror of the scope.test.ts fixture: loop-1 owns its own head, tail,
    // and 3 siblings. Tests below assert a drop inside the loop (a) only
    // moves loop siblings, (b) leaves the loop head attached to its first
    // sibling and the loop tail attached to its last sibling, and (c)
    // leaves the top-level chain (top-start ─► loop-1 ─► top-adder)
    // untouched.
    const loopNodes: Array<SortableScopeNode> = [
      start("top-start"),
      loop("loop-1"),
      start("loop-start", "loop-1"),
      block("lb1", "loop-1"),
      block("lb2", "loop-1"),
      block("lb3", "loop-1"),
      adder("loop-adder", "loop-1"),
      adder("top-adder"),
    ];
    const loopEdges: Array<Edge> = [
      edge("te-top-start", "top-start", "loop-1"),
      edge("te-loop-top-adder", "loop-1", "top-adder", "default"),
      edge("le-loop-start", "loop-start", "lb1"),
      edge("le1", "lb1", "lb2"),
      edge("le2", "lb2", "lb3"),
      edge("le3", "lb3", "loop-adder", "default"),
    ];
    const LOOP_SCOPE = { parentId: "loop-1", conditionalBranchId: null };

    test("drop lb3 above lb1 reorders only loop siblings", () => {
      const result = rewireBlockDropInScope({
        nodes: loopNodes,
        edges: loopEdges,
        scope: LOOP_SCOPE,
        activeId: "lb3",
        overId: "lb1",
      });
      expect(result).not.toBeNull();
      expect(result!.newOrder).toEqual(["lb3", "lb1", "lb2"]);
      expect(walkChain(result!.edges, "loop-start", "loop-adder")).toEqual([
        "lb3",
        "lb1",
        "lb2",
      ]);
    });

    test("loop head stays first and tail stays last after the drop", () => {
      const result = rewireBlockDropInScope({
        nodes: loopNodes,
        edges: loopEdges,
        scope: LOOP_SCOPE,
        activeId: "lb1",
        overId: "lb3",
      });
      expect(result).not.toBeNull();
      // Head edge: loop-start must still point at whatever is now the first
      // sibling (lb2 after the drop). Tail edge: the new last sibling
      // (lb1) must still terminate at loop-adder with the "default" type.
      const headTarget = result!.edges.find(
        (e) => e.source === "loop-start",
      )?.target;
      expect(headTarget).toBe("lb2");
      const tailEdge = result!.edges.find((e) => e.target === "loop-adder");
      expect(tailEdge?.source).toBe("lb1");
      expect(tailEdge?.type).toBe("default");
    });

    test("top-level chain is untouched by a drop inside a loop", () => {
      const result = rewireBlockDropInScope({
        nodes: loopNodes,
        edges: loopEdges,
        scope: LOOP_SCOPE,
        activeId: "lb2",
        overId: "lb1",
      });
      expect(result).not.toBeNull();
      // The two top-level chain edges (top-start → loop-1 and
      // loop-1 → top-adder) must survive untouched — same ids, same source
      // and target.
      const topStartEdge = result!.edges.find((e) => e.id === "te-top-start");
      expect(topStartEdge).toBeDefined();
      expect(topStartEdge!.source).toBe("top-start");
      expect(topStartEdge!.target).toBe("loop-1");
      const topTailEdge = result!.edges.find(
        (e) => e.id === "te-loop-top-adder",
      );
      expect(topTailEdge).toBeDefined();
      expect(topTailEdge!.source).toBe("loop-1");
      expect(topTailEdge!.target).toBe("top-adder");
    });

    test("drop targeting a top-level sibling from the loop scope is a no-op", () => {
      // A mis-routed drop where the over id belongs to a different scope
      // must be refused — otherwise a loop-internal drag could pull a
      // top-level block into the loop chain.
      const result = rewireBlockDropInScope({
        nodes: loopNodes,
        edges: loopEdges,
        scope: LOOP_SCOPE,
        activeId: "lb1",
        overId: "loop-1",
      });
      expect(result).toBeNull();
    });
  });

  describe("conditional-branch scope (2 branches × 2 blocks, SKY-9058)", () => {
    // Fixture matches the scope.test.ts conditional fixture and the
    // workflowEditorUtils branch-construction shape. Key facts the tests
    // below depend on:
    //
    // 1. Both branches share the same `cond-start` and `cond-adder` nodes.
    // 2. Each branch edge carries `data.conditionalBranchId` so the scope
    //    walker and rewire helper can split the shared fan-out.
    // 3. Branch A's tail edge and branch B's tail edge both point at
    //    `cond-adder` — if the scope filter weren't applied, a rewire for
    //    branch A could mistakenly pick up branch B's tail.
    const condNodes: Array<SortableScopeNode> = [
      start("top-start"),
      conditional("cond-1"),
      start("cond-start", "cond-1"),
      block("a1", "cond-1"),
      block("a2", "cond-1"),
      block("b1", "cond-1"),
      block("b2", "cond-1"),
      adder("cond-adder", "cond-1"),
      adder("top-adder"),
    ];
    const condEdges: Array<Edge> = [
      edge("te-top-start", "top-start", "cond-1"),
      edge("te-cond-top-adder", "cond-1", "top-adder", "default"),
      branchEdge("ae-cond-start-a1", "cond-start", "a1", "cond-1", "branch-a"),
      branchEdge("ae-a1-a2", "a1", "a2", "cond-1", "branch-a"),
      branchEdge(
        "ae-a2-cond-adder",
        "a2",
        "cond-adder",
        "cond-1",
        "branch-a",
        "default",
      ),
      branchEdge("be-cond-start-b1", "cond-start", "b1", "cond-1", "branch-b"),
      branchEdge("be-b1-b2", "b1", "b2", "cond-1", "branch-b"),
      branchEdge(
        "be-b2-cond-adder",
        "b2",
        "cond-adder",
        "cond-1",
        "branch-b",
        "default",
      ),
    ];
    const BRANCH_A_SCOPE = {
      parentId: "cond-1",
      conditionalBranchId: "branch-a",
    };
    const BRANCH_B_SCOPE = {
      parentId: "cond-1",
      conditionalBranchId: "branch-b",
    };

    // walkChain is branch-agnostic (picks the first outgoing edge by source),
    // so assertions that need to stay inside one branch filter on
    // `data.conditionalBranchId` instead.
    function walkBranchChain(
      edges: Array<Edge>,
      startId: string,
      adderId: string,
      branchId: string,
    ): Array<string> {
      const order: Array<string> = [];
      const visited = new Set<string>();
      const matches = (e: Edge): boolean =>
        (e.data as { conditionalBranchId?: string } | undefined)
          ?.conditionalBranchId === branchId;
      let next = edges.find((e) => e.source === startId && matches(e))?.target;
      while (next && next !== adderId && !visited.has(next)) {
        visited.add(next);
        order.push(next);
        const currentNext = next;
        next = edges.find(
          (e) => e.source === currentNext && matches(e),
        )?.target;
      }
      return order;
    }

    test("drop a2 above a1 reorders only branch A's siblings", () => {
      const result = rewireBlockDropInScope({
        nodes: condNodes,
        edges: condEdges,
        scope: BRANCH_A_SCOPE,
        activeId: "a2",
        overId: "a1",
      });
      expect(result).not.toBeNull();
      expect(result!.newOrder).toEqual(["a2", "a1"]);
      expect(
        walkBranchChain(result!.edges, "cond-start", "cond-adder", "branch-a"),
      ).toEqual(["a2", "a1"]);
      // Branch B's chain walks unchanged.
      expect(
        walkBranchChain(result!.edges, "cond-start", "cond-adder", "branch-b"),
      ).toEqual(["b1", "b2"]);
    });

    test("branch B chain is untouched when dropping inside branch A", () => {
      const result = rewireBlockDropInScope({
        nodes: condNodes,
        edges: condEdges,
        scope: BRANCH_A_SCOPE,
        activeId: "a2",
        overId: "a1",
      });
      expect(result).not.toBeNull();
      const branchBTailIds = [
        "be-cond-start-b1",
        "be-b1-b2",
        "be-b2-cond-adder",
      ];
      for (const id of branchBTailIds) {
        const e = result!.edges.find((edge) => edge.id === id);
        expect(e).toBeDefined();
      }
      // Edge ids, sources, targets, and types for branch B are preserved
      // exactly — only branch A's chain should have been rewired.
      const beCondStartB1 = result!.edges.find(
        (e) => e.id === "be-cond-start-b1",
      );
      expect(beCondStartB1?.source).toBe("cond-start");
      expect(beCondStartB1?.target).toBe("b1");
      const beB2CondAdder = result!.edges.find(
        (e) => e.id === "be-b2-cond-adder",
      );
      expect(beB2CondAdder?.source).toBe("b2");
      expect(beB2CondAdder?.target).toBe("cond-adder");
      expect(beB2CondAdder?.type).toBe("default");
    });

    test("branch A rewire preserves conditional-branch data on the rewired edges", () => {
      // The rewire helper splices new chain positions but must reuse the
      // existing edge objects — including their `data.conditionalBranchId`
      // and `data.conditionalNodeId` tags — so the scope walker can still
      // follow the chain after a drop.
      const result = rewireBlockDropInScope({
        nodes: condNodes,
        edges: condEdges,
        scope: BRANCH_A_SCOPE,
        activeId: "a2",
        overId: "a1",
      });
      expect(result).not.toBeNull();
      const branchAEdges = result!.edges.filter((e) => {
        const data = e.data as
          | { conditionalBranchId?: string; conditionalNodeId?: string }
          | undefined;
        return data?.conditionalBranchId === "branch-a";
      });
      // Start→first, middle, last→adder
      expect(branchAEdges).toHaveLength(3);
      for (const e of branchAEdges) {
        const data = e.data as { conditionalNodeId?: string };
        expect(data.conditionalNodeId).toBe("cond-1");
      }
    });

    test("shared cond-start and cond-adder endpoints stay attached to branch A", () => {
      const result = rewireBlockDropInScope({
        nodes: condNodes,
        edges: condEdges,
        scope: BRANCH_A_SCOPE,
        activeId: "a1",
        overId: "a2",
      });
      expect(result).not.toBeNull();
      // After the drop, branch A order is [a2, a1] — cond-start should now
      // point at a2, and a1 should be the branch's tail into cond-adder.
      const branchAHead = result!.edges.find(
        (e) =>
          e.source === "cond-start" &&
          (e.data as { conditionalBranchId?: string } | undefined)
            ?.conditionalBranchId === "branch-a",
      );
      expect(branchAHead?.target).toBe("a2");
      const branchATail = result!.edges.find(
        (e) =>
          e.target === "cond-adder" &&
          (e.data as { conditionalBranchId?: string } | undefined)
            ?.conditionalBranchId === "branch-a",
      );
      expect(branchATail?.source).toBe("a1");
      expect(branchATail?.type).toBe("default");
    });

    test("cross-branch drop (branch A scope, over id from branch B) is rejected", () => {
      // The AC scope guard: a drag that originates in branch A but somehow
      // routes an over id from branch B must be refused — otherwise a
      // branch A block could be spliced into branch B's chain and split the
      // saved workflow across two branches.
      const result = rewireBlockDropInScope({
        nodes: condNodes,
        edges: condEdges,
        scope: BRANCH_A_SCOPE,
        activeId: "a1",
        overId: "b1",
      });
      expect(result).toBeNull();
    });

    test("cross-branch drop rejected in the reverse direction too", () => {
      const result = rewireBlockDropInScope({
        nodes: condNodes,
        edges: condEdges,
        scope: BRANCH_B_SCOPE,
        activeId: "b2",
        overId: "a1",
      });
      expect(result).toBeNull();
    });

    test("drop targeting the shared cond-adder from a branch scope is a no-op", () => {
      // cond-adder is the branch's tail anchor, not a sibling. Attempting
      // to drop onto it must be refused just like a drop onto the
      // top-level NodeAdder is refused.
      const result = rewireBlockDropInScope({
        nodes: condNodes,
        edges: condEdges,
        scope: BRANCH_A_SCOPE,
        activeId: "a1",
        overId: "cond-adder",
      });
      expect(result).toBeNull();
    });

    test("top-level chain is untouched by a drop inside a conditional branch", () => {
      const result = rewireBlockDropInScope({
        nodes: condNodes,
        edges: condEdges,
        scope: BRANCH_A_SCOPE,
        activeId: "a2",
        overId: "a1",
      });
      expect(result).not.toBeNull();
      const topStartEdge = result!.edges.find((e) => e.id === "te-top-start");
      expect(topStartEdge?.source).toBe("top-start");
      expect(topStartEdge?.target).toBe("cond-1");
      const topTailEdge = result!.edges.find(
        (e) => e.id === "te-cond-top-adder",
      );
      expect(topTailEdge?.source).toBe("cond-1");
      expect(topTailEdge?.target).toBe("top-adder");
    });

    test("edge count is preserved across a branch-scope rewire", () => {
      const result = rewireBlockDropInScope({
        nodes: condNodes,
        edges: condEdges,
        scope: BRANCH_A_SCOPE,
        activeId: "a1",
        overId: "a2",
      });
      expect(result).not.toBeNull();
      expect(result!.edges.length).toBe(condEdges.length);
    });
  });

  describe("finally-block gate (top-level, SKY-9060)", () => {
    // Fixture: 4-block top-level chain where `d` is flagged as the finally
    // block. Under the invariant enforced by NodeAdderNode's disable gate
    // (`!parentId && workflowSettingsStore.finallyBlockLabel`), the finally
    // block must always be the trailing sibling. These tests extend that
    // same guard to drag/drop.
    const finallyId = "d";

    test("drop that would displace the finally block backward is rejected", () => {
      // Drop `a` (head) at `d`'s slot: without the gate arrayMove would
      // produce [b, c, d, a], shifting the finally block off the tail.
      const result = rewireBlockDropInScope({
        nodes,
        edges: baseEdges,
        scope: TOP_LEVEL_SCOPE,
        activeId: "a",
        overId: finallyId,
        finallyBlockId: finallyId,
      });
      expect(result).toBeNull();
    });

    test("drop onto the finally block is rejected regardless of direction", () => {
      // Same rejection when the dragged block sits closer to the tail.
      const result = rewireBlockDropInScope({
        nodes,
        edges: baseEdges,
        scope: TOP_LEVEL_SCOPE,
        activeId: "b",
        overId: finallyId,
        finallyBlockId: finallyId,
      });
      expect(result).toBeNull();
    });

    test("dragging the finally block itself is rejected", () => {
      // Even if the grip handle leaks through (a11y programmatic drag,
      // future wiring regression), the rewire helper refuses to move the
      // finally block out of its tail slot.
      const result = rewireBlockDropInScope({
        nodes,
        edges: baseEdges,
        scope: TOP_LEVEL_SCOPE,
        activeId: finallyId,
        overId: "a",
        finallyBlockId: finallyId,
      });
      expect(result).toBeNull();
    });

    test("chain edges untouched when a finally-displacing drop is refused", () => {
      // A rejected drop must not mutate any edge — the top-level chain
      // should stay at [a, b, c, d] with the `d → adder` tail intact. We
      // re-run the walk on the original edges (sanity) and confirm the
      // rewire returned null so the caller short-circuits before doLayout.
      const rejected = rewireBlockDropInScope({
        nodes,
        edges: baseEdges,
        scope: TOP_LEVEL_SCOPE,
        activeId: "a",
        overId: finallyId,
        finallyBlockId: finallyId,
      });
      expect(rejected).toBeNull();
      expect(walkChain(baseEdges, "start", "adder")).toEqual([
        "a",
        "b",
        "c",
        "d",
      ]);
      expect(chainEdgeType(baseEdges, "d", "adder")).toBe("default");
    });

    test("valid reorder before the finally block is allowed", () => {
      // Swapping `a` and `c` keeps `d` in the tail slot, so the gate must
      // let the drop through. This proves the gate isn't over-eager.
      const result = rewireBlockDropInScope({
        nodes,
        edges: baseEdges,
        scope: TOP_LEVEL_SCOPE,
        activeId: "a",
        overId: "c",
        finallyBlockId: finallyId,
      });
      expect(result).not.toBeNull();
      expect(result!.newOrder).toEqual(["b", "c", "a", "d"]);
      expect(walkChain(result!.edges, "start", "adder")).toEqual([
        "b",
        "c",
        "a",
        "d",
      ]);
      // Finally block remains the tail into the NodeAdder.
      expect(chainEdgeType(result!.edges, "d", "adder")).toBe("default");
    });

    test("gate is a no-op when finallyBlockId is not set", () => {
      // Caller passes null when the workflow has no finally block — the
      // rewire must behave identically to the default (pre-gate) path.
      const result = rewireBlockDropInScope({
        nodes,
        edges: baseEdges,
        scope: TOP_LEVEL_SCOPE,
        activeId: "a",
        overId: "d",
        finallyBlockId: null,
      });
      expect(result).not.toBeNull();
      expect(result!.newOrder).toEqual(["b", "c", "d", "a"]);
    });

    test("gate does not apply to loop-scope reorders", () => {
      // `finallyBlockLabel` is a workflow-root concept; a drag inside a
      // loop container must not be gated even if the caller accidentally
      // passes a finallyBlockId (defensive check against future misuse).
      const loopNodes: Array<SortableScopeNode> = [
        start("top-start"),
        loop("loop-1"),
        start("loop-start", "loop-1"),
        block("lb1", "loop-1"),
        block("lb2", "loop-1"),
        block("lb3", "loop-1"),
        adder("loop-adder", "loop-1"),
        adder("top-adder"),
      ];
      const loopEdges: Array<Edge> = [
        edge("te-top-start", "top-start", "loop-1"),
        edge("te-loop-top-adder", "loop-1", "top-adder", "default"),
        edge("le-loop-start", "loop-start", "lb1"),
        edge("le1", "lb1", "lb2"),
        edge("le2", "lb2", "lb3"),
        edge("le3", "lb3", "loop-adder", "default"),
      ];
      const result = rewireBlockDropInScope({
        nodes: loopNodes,
        edges: loopEdges,
        scope: { parentId: "loop-1", conditionalBranchId: null },
        activeId: "lb1",
        overId: "lb3",
        // Deliberately set a finallyBlockId — a loop-scope drop must ignore
        // it. This protects the invariant that nested-scope reorders are
        // unaffected by the top-level finally gate.
        finallyBlockId: "lb3",
      });
      expect(result).not.toBeNull();
      expect(result!.newOrder).toEqual(["lb2", "lb3", "lb1"]);
    });
  });

  test("rewire preserves siblings so findNextBlockLabel chain reflects new order", () => {
    // findNextBlockLabel (workflowEditorUtils.ts:2137) derives the saved
    // next_block_label chain by walking edges from each block's outgoing
    // chain edge. We simulate that walk here using the rewired edges and
    // assert the chain matches the dropped order — this is the ground truth
    // that protects the save-time semantics without importing the full
    // util (which would require an AppNode fixture).
    const result = rewireBlockDropInScope({
      nodes,
      edges: baseEdges,
      scope: TOP_LEVEL_SCOPE,
      activeId: "b",
      overId: "a",
    });
    expect(result).not.toBeNull();
    // Walk: for each sibling in new order, the edge from it points to the
    // next sibling (or the adder for the tail), which is exactly what
    // findNextBlockLabel resolves via its outgoing-edge lookup.
    const chain = walkChain(result!.edges, "start", "adder");
    expect(chain).toEqual(["b", "a", "c", "d"]);
    expect(chainEdgeType(result!.edges, "d", "adder")).toBe("default");
  });
});

describe("classifyBlockDrop (SKY-9062)", () => {
  // The classifier is the data source for the unified drop-blocked toast
  // (FlowRenderer.onDndDragEnd -> showDropBlockedToast). These tests pin
  // the `kind` / `reason` discriminants so the toast always fires for
  // user-meaningful rejections and stays silent for legitimate no-ops.
  const finallyId = "d";

  describe("ok outcome", () => {
    test("returns ok with edges + newOrder for a valid reorder", () => {
      const outcome = classifyBlockDrop({
        nodes,
        edges: baseEdges,
        scope: TOP_LEVEL_SCOPE,
        activeId: "b",
        overId: "a",
      });
      expect(outcome.kind).toBe("ok");
      if (outcome.kind !== "ok") return;
      expect(outcome.newOrder).toEqual(["b", "a", "c", "d"]);
      expect(outcome.edges).toBeDefined();
    });
  });

  describe("noop outcome (no toast)", () => {
    // Each case below is a release-without-move that users do by accident.
    // Surfacing a toast for these would be noise — the classifier returns
    // `noop` so the handler stays silent.
    test("drop on self is a noop", () => {
      const outcome = classifyBlockDrop({
        nodes,
        edges: baseEdges,
        scope: TOP_LEVEL_SCOPE,
        activeId: "b",
        overId: "b",
      });
      expect(outcome.kind).toBe("noop");
    });

    test("null over id is a noop", () => {
      const outcome = classifyBlockDrop({
        nodes,
        edges: baseEdges,
        scope: TOP_LEVEL_SCOPE,
        activeId: "b",
        overId: null,
      });
      expect(outcome.kind).toBe("noop");
    });

    test("unknown active id is a noop (not blocked)", () => {
      // The classifier sees an active id that isn't in the scope — that's
      // a structural issue from the caller, not a user-facing rule. Noop
      // so the toast doesn't fire on transient mid-drag deletes.
      const outcome = classifyBlockDrop({
        nodes,
        edges: baseEdges,
        scope: TOP_LEVEL_SCOPE,
        activeId: "unknown",
        overId: "a",
      });
      expect(outcome.kind).toBe("noop");
    });

    test("empty scope is a noop", () => {
      const emptyNodes: Array<SortableScopeNode> = [
        start("start"),
        adder("adder"),
      ];
      const emptyEdges: Array<Edge> = [edge("e0", "start", "adder", "default")];
      const outcome = classifyBlockDrop({
        nodes: emptyNodes,
        edges: emptyEdges,
        scope: TOP_LEVEL_SCOPE,
        activeId: "x",
        overId: "y",
      });
      expect(outcome.kind).toBe("noop");
    });
  });

  describe("blocked: finally-pin", () => {
    test("dropping another block onto the finally slot is blocked with finally-pin", () => {
      const outcome = classifyBlockDrop({
        nodes,
        edges: baseEdges,
        scope: TOP_LEVEL_SCOPE,
        activeId: "a",
        overId: finallyId,
        finallyBlockId: finallyId,
      });
      expect(outcome.kind).toBe("blocked");
      if (outcome.kind !== "blocked") return;
      expect(outcome.reason).toBe("finally-pin");
    });

    test("dragging the finally block itself is blocked with finally-pin", () => {
      // Even if the a11y handle leaks through, the classifier refuses to
      // dislodge the finally block — and names the reason so the toast
      // can tell the user why.
      const outcome = classifyBlockDrop({
        nodes,
        edges: baseEdges,
        scope: TOP_LEVEL_SCOPE,
        activeId: finallyId,
        overId: "a",
        finallyBlockId: finallyId,
      });
      expect(outcome.kind).toBe("blocked");
      if (outcome.kind !== "blocked") return;
      expect(outcome.reason).toBe("finally-pin");
    });
  });

  describe("blocked: cross-scope", () => {
    test("top-level over id on a loop-scope drag is cross-scope", () => {
      // A drag originating inside a loop container that somehow targets a
      // top-level block must not splice across scopes. The classifier
      // surfaces `cross-scope` so the toast names the grouping rule.
      const loopNodes: Array<SortableScopeNode> = [
        start("top-start"),
        loop("loop-1"),
        start("loop-start", "loop-1"),
        block("lb1", "loop-1"),
        block("lb2", "loop-1"),
        adder("loop-adder", "loop-1"),
        adder("top-adder"),
      ];
      const loopEdges: Array<Edge> = [
        edge("te-top-start", "top-start", "loop-1"),
        edge("te-loop-top-adder", "loop-1", "top-adder", "default"),
        edge("le-loop-start", "loop-start", "lb1"),
        edge("le1", "lb1", "lb2"),
        edge("le2", "lb2", "loop-adder", "default"),
      ];
      const outcome = classifyBlockDrop({
        nodes: loopNodes,
        edges: loopEdges,
        scope: { parentId: "loop-1", conditionalBranchId: null },
        activeId: "lb1",
        // Deliberately an over id that exists but isn't a loop sibling.
        overId: "loop-1",
      });
      expect(outcome.kind).toBe("blocked");
      if (outcome.kind !== "blocked") return;
      expect(outcome.reason).toBe("cross-scope");
    });

    test("over id from a sibling branch is cross-scope", () => {
      // Mirrors the existing conditional-branch fixture: branch A scope,
      // over id from branch B. The rewire wrapper still returns null; the
      // classifier differentiates the reason so the user gets a toast.
      const condNodes: Array<SortableScopeNode> = [
        start("top-start"),
        conditional("cond-1"),
        start("cond-start", "cond-1"),
        block("a1", "cond-1"),
        block("b1", "cond-1"),
        adder("cond-adder", "cond-1"),
        adder("top-adder"),
      ];
      const condEdges: Array<Edge> = [
        edge("te-top-start", "top-start", "cond-1"),
        edge("te-cond-top-adder", "cond-1", "top-adder", "default"),
        branchEdge(
          "ae-cond-start-a1",
          "cond-start",
          "a1",
          "cond-1",
          "branch-a",
        ),
        branchEdge(
          "ae-a1-cond-adder",
          "a1",
          "cond-adder",
          "cond-1",
          "branch-a",
          "default",
        ),
        branchEdge(
          "be-cond-start-b1",
          "cond-start",
          "b1",
          "cond-1",
          "branch-b",
        ),
        branchEdge(
          "be-b1-cond-adder",
          "b1",
          "cond-adder",
          "cond-1",
          "branch-b",
          "default",
        ),
      ];
      const outcome = classifyBlockDrop({
        nodes: condNodes,
        edges: condEdges,
        scope: { parentId: "cond-1", conditionalBranchId: "branch-a" },
        activeId: "a1",
        overId: "b1",
      });
      expect(outcome.kind).toBe("blocked");
      if (outcome.kind !== "blocked") return;
      expect(outcome.reason).toBe("cross-scope");
    });
  });

  describe("rewireBlockDropInScope ↔ classifyBlockDrop parity", () => {
    test("rewire returns null whenever classify returns non-ok", () => {
      // `rewireBlockDropInScope` is now a thin wrapper over the classifier.
      // This check guards against drift between the two: the legacy
      // callers / tests relying on the null behavior stay honest only if
      // the wrapper maps every non-`ok` outcome to `null`.
      const cases: Array<{
        activeId: string;
        overId: string | null;
        finallyBlockId?: string;
      }> = [
        { activeId: "b", overId: "b" }, // noop: self
        { activeId: "b", overId: null }, // noop: null over
        { activeId: "unknown", overId: "a" }, // noop: unknown active
        {
          activeId: "a",
          overId: finallyId,
          finallyBlockId: finallyId,
        }, // blocked: finally-pin
      ];
      for (const c of cases) {
        const result = rewireBlockDropInScope({
          nodes,
          edges: baseEdges,
          scope: TOP_LEVEL_SCOPE,
          ...c,
        });
        expect(result).toBeNull();
      }
    });

    test("rewire returns a result whenever classify returns ok", () => {
      const outcome = classifyBlockDrop({
        nodes,
        edges: baseEdges,
        scope: TOP_LEVEL_SCOPE,
        activeId: "c",
        overId: "a",
      });
      expect(outcome.kind).toBe("ok");
      const result = rewireBlockDropInScope({
        nodes,
        edges: baseEdges,
        scope: TOP_LEVEL_SCOPE,
        activeId: "c",
        overId: "a",
      });
      expect(result).not.toBeNull();
      if (outcome.kind === "ok" && result) {
        expect(result.newOrder).toEqual(outcome.newOrder);
      }
    });
  });
});
