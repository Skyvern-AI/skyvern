import type { Edge } from "@xyflow/react";
import { describe, expect, test } from "vitest";

import {
  TOP_LEVEL_SCOPE,
  TOP_LEVEL_SCOPE_KEY,
  collectConditionalBranchScopes,
  collectLoopScopes,
  getOrderedBlockIdsAtScope,
  getScopeKey,
  type SortableScopeNode,
} from "./scope";

function start(id: string, parentId?: string): SortableScopeNode {
  return parentId === undefined
    ? { id, type: "start" }
    : { id, type: "start", parentId };
}

// Representative sortable block type — any non-start, non-nodeAdder type
// walks the same scope path, so `task` stands in for the full block taxonomy.
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

function edge(source: string, target: string): Edge {
  return { id: `${source}->${target}`, source, target };
}

/**
 * Edge with the conditional-branch data that workflowEditorUtils attaches
 * when building branch chains. The scope walker uses these `data` tags to
 * pick the correct outgoing edge when multiple branches share a start node.
 */
function branchEdge(
  source: string,
  target: string,
  conditionalNodeId: string,
  conditionalBranchId: string,
): Edge {
  return {
    id: `${source}->${target}:${conditionalBranchId}`,
    source,
    target,
    data: { conditionalNodeId, conditionalBranchId },
  };
}

describe("getScopeKey", () => {
  test("top-level scope produces the shared top-level key", () => {
    expect(getScopeKey(TOP_LEVEL_SCOPE)).toBe(TOP_LEVEL_SCOPE_KEY);
  });

  test("nested scope encodes parentId and branchId", () => {
    expect(getScopeKey({ parentId: "loop-1", conditionalBranchId: null })).toBe(
      "scope:loop-1:__main__",
    );
    expect(
      getScopeKey({ parentId: "cond-1", conditionalBranchId: "branch-a" }),
    ).toBe("scope:cond-1:branch-a");
  });

  test("distinct scopes produce distinct keys", () => {
    const a = getScopeKey({ parentId: "loop-1", conditionalBranchId: null });
    const b = getScopeKey({ parentId: "loop-2", conditionalBranchId: null });
    expect(a).not.toBe(b);
  });
});

describe("getOrderedBlockIdsAtScope", () => {
  test("returns empty list when no matching start node exists", () => {
    expect(
      getOrderedBlockIdsAtScope({
        nodes: [],
        edges: [],
        scope: TOP_LEVEL_SCOPE,
      }),
    ).toEqual([]);
  });

  test("walks the top-level chain and stops at the node adder", () => {
    const nodes: Array<SortableScopeNode> = [
      start("start"),
      block("a"),
      block("b"),
      block("c"),
      adder("adder"),
    ];
    const edges: Array<Edge> = [
      edge("start", "a"),
      edge("a", "b"),
      edge("b", "c"),
      edge("c", "adder"),
    ];
    expect(
      getOrderedBlockIdsAtScope({ nodes, edges, scope: TOP_LEVEL_SCOPE }),
    ).toEqual(["a", "b", "c"]);
  });

  test("skips blocks that live inside a loop (different parentId)", () => {
    const nodes: Array<SortableScopeNode> = [
      start("top-start"),
      block("loop"),
      block("inside", "loop"),
      block("after"),
      adder("adder"),
    ];
    const edges: Array<Edge> = [
      edge("top-start", "loop"),
      edge("loop", "after"),
      edge("after", "adder"),
    ];
    expect(
      getOrderedBlockIdsAtScope({ nodes, edges, scope: TOP_LEVEL_SCOPE }),
    ).toEqual(["loop", "after"]);
  });

  test("top-level scope does not match a non-root start node", () => {
    const nodes: Array<SortableScopeNode> = [
      start("nested-start", "loop-1"),
      block("only-inside", "loop-1"),
    ];
    const edges: Array<Edge> = [edge("nested-start", "only-inside")];
    expect(
      getOrderedBlockIdsAtScope({ nodes, edges, scope: TOP_LEVEL_SCOPE }),
    ).toEqual([]);
  });

  test("top-level scope with an empty chain (only start + adder) returns []", () => {
    const nodes: Array<SortableScopeNode> = [start("start"), adder("adder")];
    const edges: Array<Edge> = [edge("start", "adder")];
    expect(
      getOrderedBlockIdsAtScope({ nodes, edges, scope: TOP_LEVEL_SCOPE }),
    ).toEqual([]);
  });

  test("terminates when the edge graph contains a cycle", () => {
    // The walker's `visited` set guards against malformed edge graphs
    // (cycles shouldn't exist in a healthy workflow, but if one does we
    // must still terminate in bounded time rather than spin forever).
    const nodes: Array<SortableScopeNode> = [
      start("start"),
      block("a"),
      block("b"),
    ];
    const edges: Array<Edge> = [
      edge("start", "a"),
      edge("a", "b"),
      edge("b", "a"), // cycle back to a
    ];
    expect(
      getOrderedBlockIdsAtScope({ nodes, edges, scope: TOP_LEVEL_SCOPE }),
    ).toEqual(["a", "b"]);
  });
});

describe("loop-container scope (3-block fixture)", () => {
  // Fixture used across the next several tests (SKY-9057 AC):
  //
  //   top-start ─► loop-1 ─► top-adder
  //                  │
  //                  └─ (owned): loop-start ─► b1 ─► b2 ─► b3 ─► loop-adder
  //
  // Loop head (__start_block__) and tail (NodeAdderNode) both live under
  // parentId = "loop-1", so the scope descriptor { parentId: "loop-1" }
  // resolves the loop's own sibling chain while TOP_LEVEL_SCOPE stays
  // anchored to `top-start`.
  function buildThreeBlockLoopFixture(): {
    nodes: Array<SortableScopeNode>;
    edges: Array<Edge>;
  } {
    const nodes: Array<SortableScopeNode> = [
      start("top-start"),
      loop("loop-1"),
      start("loop-start", "loop-1"),
      block("b1", "loop-1"),
      block("b2", "loop-1"),
      block("b3", "loop-1"),
      adder("loop-adder", "loop-1"),
      adder("top-adder"),
    ];
    const edges: Array<Edge> = [
      edge("top-start", "loop-1"),
      edge("loop-1", "top-adder"),
      edge("loop-start", "b1"),
      edge("b1", "b2"),
      edge("b2", "b3"),
      edge("b3", "loop-adder"),
    ];
    return { nodes, edges };
  }

  const LOOP_SCOPE = { parentId: "loop-1", conditionalBranchId: null };

  test("loop scope walks only the loop's sibling chain (3-block fixture)", () => {
    const { nodes, edges } = buildThreeBlockLoopFixture();
    expect(
      getOrderedBlockIdsAtScope({ nodes, edges, scope: LOOP_SCOPE }),
    ).toEqual(["b1", "b2", "b3"]);
  });

  test("loop scope excludes the head __start_block__ and tail NodeAdderNode", () => {
    const { nodes, edges } = buildThreeBlockLoopFixture();
    const order = getOrderedBlockIdsAtScope({
      nodes,
      edges,
      scope: LOOP_SCOPE,
    });
    expect(order).not.toContain("loop-start");
    expect(order).not.toContain("loop-adder");
  });

  test("top-level scope does not leak loop children when a loop is present", () => {
    const { nodes, edges } = buildThreeBlockLoopFixture();
    expect(
      getOrderedBlockIdsAtScope({ nodes, edges, scope: TOP_LEVEL_SCOPE }),
    ).toEqual(["loop-1"]);
  });

  test("loop scope and top-level scope produce disjoint orderings", () => {
    const { nodes, edges } = buildThreeBlockLoopFixture();
    const topLevel = getOrderedBlockIdsAtScope({
      nodes,
      edges,
      scope: TOP_LEVEL_SCOPE,
    });
    const loopOrder = getOrderedBlockIdsAtScope({
      nodes,
      edges,
      scope: LOOP_SCOPE,
    });
    for (const id of topLevel) {
      expect(loopOrder).not.toContain(id);
    }
  });

  test("an empty loop (start → adder, no siblings) returns []", () => {
    const nodes: Array<SortableScopeNode> = [
      start("top-start"),
      loop("loop-1"),
      start("loop-start", "loop-1"),
      adder("loop-adder", "loop-1"),
      adder("top-adder"),
    ];
    const edges: Array<Edge> = [
      edge("top-start", "loop-1"),
      edge("loop-1", "top-adder"),
      edge("loop-start", "loop-adder"),
    ];
    expect(
      getOrderedBlockIdsAtScope({ nodes, edges, scope: LOOP_SCOPE }),
    ).toEqual([]);
  });

  test("a loop scope with no matching start node returns []", () => {
    // Malformed fixture: loop-1 exists but has no nested start. The scope
    // walker must refuse to fabricate a chain rather than walking top-level
    // edges or producing partial data.
    const nodes: Array<SortableScopeNode> = [
      start("top-start"),
      loop("loop-1"),
      block("b1", "loop-1"),
    ];
    const edges: Array<Edge> = [
      edge("top-start", "loop-1"),
      edge("loop-1", "b1"),
    ];
    expect(
      getOrderedBlockIdsAtScope({ nodes, edges, scope: LOOP_SCOPE }),
    ).toEqual([]);
  });
});

describe("collectLoopScopes", () => {
  test("returns one scope per loop container", () => {
    const nodes: Array<SortableScopeNode> = [
      start("top-start"),
      loop("loop-1"),
      loop("loop-2"),
      block("b1"),
    ];
    expect(collectLoopScopes(nodes)).toEqual([
      { parentId: "loop-1", conditionalBranchId: null },
      { parentId: "loop-2", conditionalBranchId: null },
    ]);
  });

  test("returns an empty list when no loops are present", () => {
    const nodes: Array<SortableScopeNode> = [
      start("start"),
      block("a"),
      block("b"),
      adder("adder"),
    ];
    expect(collectLoopScopes(nodes)).toEqual([]);
  });

  test("each collected scope has a unique key", () => {
    const nodes: Array<SortableScopeNode> = [loop("loop-1"), loop("loop-2")];
    const scopes = collectLoopScopes(nodes);
    const keys = scopes.map(getScopeKey);
    expect(new Set(keys).size).toBe(keys.length);
  });
});

describe("conditional-branch scope (2 branches × 2 blocks fixture)", () => {
  // Fixture mirrors workflowEditorUtils.buildConditionalStructure output for
  // the SKY-9058 AC (2 branches, 2 blocks each):
  //
  //   top-start ─► cond-1 ─► top-adder
  //                  │
  //                  │ (cond-1's shared start + shared adder)
  //                  └ cond-start ─┬─ [branch-a] ─► a1 ─► a2 ─► cond-adder
  //                                └─ [branch-b] ─► b1 ─► b2 ─► cond-adder
  //
  // Branch edges carry data.conditionalBranchId so the scope walker can pick
  // the correct outgoing edge when the shared start fans out to two
  // branches. Top-level edges (top-start → cond-1, cond-1 → top-adder)
  // stay untagged.
  function buildTwoBranchConditionalFixture(): {
    nodes: Array<SortableScopeNode>;
    edges: Array<Edge>;
  } {
    const nodes: Array<SortableScopeNode> = [
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
    const edges: Array<Edge> = [
      edge("top-start", "cond-1"),
      edge("cond-1", "top-adder"),
      branchEdge("cond-start", "a1", "cond-1", "branch-a"),
      branchEdge("a1", "a2", "cond-1", "branch-a"),
      branchEdge("a2", "cond-adder", "cond-1", "branch-a"),
      branchEdge("cond-start", "b1", "cond-1", "branch-b"),
      branchEdge("b1", "b2", "cond-1", "branch-b"),
      branchEdge("b2", "cond-adder", "cond-1", "branch-b"),
    ];
    return { nodes, edges };
  }

  const BRANCH_A_SCOPE = {
    parentId: "cond-1",
    conditionalBranchId: "branch-a",
  };
  const BRANCH_B_SCOPE = {
    parentId: "cond-1",
    conditionalBranchId: "branch-b",
  };

  test("branch A scope walks only branch A's siblings", () => {
    const { nodes, edges } = buildTwoBranchConditionalFixture();
    expect(
      getOrderedBlockIdsAtScope({ nodes, edges, scope: BRANCH_A_SCOPE }),
    ).toEqual(["a1", "a2"]);
  });

  test("branch B scope walks only branch B's siblings", () => {
    const { nodes, edges } = buildTwoBranchConditionalFixture();
    expect(
      getOrderedBlockIdsAtScope({ nodes, edges, scope: BRANCH_B_SCOPE }),
    ).toEqual(["b1", "b2"]);
  });

  test("branch A and branch B orderings are disjoint", () => {
    const { nodes, edges } = buildTwoBranchConditionalFixture();
    const orderA = getOrderedBlockIdsAtScope({
      nodes,
      edges,
      scope: BRANCH_A_SCOPE,
    });
    const orderB = getOrderedBlockIdsAtScope({
      nodes,
      edges,
      scope: BRANCH_B_SCOPE,
    });
    for (const id of orderA) {
      expect(orderB).not.toContain(id);
    }
  });

  test("branch scope excludes the shared start and shared adder", () => {
    const { nodes, edges } = buildTwoBranchConditionalFixture();
    const order = getOrderedBlockIdsAtScope({
      nodes,
      edges,
      scope: BRANCH_A_SCOPE,
    });
    expect(order).not.toContain("cond-start");
    expect(order).not.toContain("cond-adder");
  });

  test("top-level scope is not affected by branch-tagged edges", () => {
    const { nodes, edges } = buildTwoBranchConditionalFixture();
    expect(
      getOrderedBlockIdsAtScope({ nodes, edges, scope: TOP_LEVEL_SCOPE }),
    ).toEqual(["cond-1"]);
  });

  test("an empty branch (no siblings, only START → adder) returns []", () => {
    // Branch A populated, branch B empty — models the state right after a
    // user adds a new branch via the conditional tabs before dropping any
    // blocks into it.
    const nodes: Array<SortableScopeNode> = [
      start("top-start"),
      conditional("cond-1"),
      start("cond-start", "cond-1"),
      block("a1", "cond-1"),
      adder("cond-adder", "cond-1"),
      adder("top-adder"),
    ];
    const edges: Array<Edge> = [
      edge("top-start", "cond-1"),
      edge("cond-1", "top-adder"),
      branchEdge("cond-start", "a1", "cond-1", "branch-a"),
      branchEdge("a1", "cond-adder", "cond-1", "branch-a"),
      branchEdge("cond-start", "cond-adder", "cond-1", "branch-b"),
    ];
    expect(
      getOrderedBlockIdsAtScope({ nodes, edges, scope: BRANCH_B_SCOPE }),
    ).toEqual([]);
    expect(
      getOrderedBlockIdsAtScope({ nodes, edges, scope: BRANCH_A_SCOPE }),
    ).toEqual(["a1"]);
  });

  test("branch scope rejects a malformed descriptor with null parentId", () => {
    // A branch scope must have a conditional parentId. A null parentId with
    // a non-null branch id is nonsensical — the top-level chain has no
    // branches — so the walker refuses to resolve a start rather than
    // accidentally walking the workflow-settings root chain.
    const { nodes, edges } = buildTwoBranchConditionalFixture();
    expect(
      getOrderedBlockIdsAtScope({
        nodes,
        edges,
        scope: { parentId: null, conditionalBranchId: "branch-a" },
      }),
    ).toEqual([]);
  });
});

describe("collectConditionalBranchScopes", () => {
  test("returns one scope per (conditionalNode, branch) pair", () => {
    const nodes: Array<SortableScopeNode> = [
      conditional("cond-1"),
      conditional("cond-2"),
      block("a1", "cond-1"),
      block("b1", "cond-1"),
      block("c1", "cond-2"),
    ];
    const edges: Array<Edge> = [
      branchEdge("cond-1-start", "a1", "cond-1", "branch-a"),
      branchEdge("a1", "adder-1", "cond-1", "branch-a"),
      branchEdge("cond-1-start", "b1", "cond-1", "branch-b"),
      branchEdge("b1", "adder-1", "cond-1", "branch-b"),
      branchEdge("cond-2-start", "c1", "cond-2", "only-branch"),
      branchEdge("c1", "adder-2", "cond-2", "only-branch"),
    ];
    const scopes = collectConditionalBranchScopes(nodes, edges);
    expect(scopes).toEqual(
      expect.arrayContaining([
        { parentId: "cond-1", conditionalBranchId: "branch-a" },
        { parentId: "cond-1", conditionalBranchId: "branch-b" },
        { parentId: "cond-2", conditionalBranchId: "only-branch" },
      ]),
    );
    expect(scopes).toHaveLength(3);
  });

  test("each collected branch scope has a unique key", () => {
    const nodes: Array<SortableScopeNode> = [
      conditional("cond-1"),
      conditional("cond-2"),
    ];
    const edges: Array<Edge> = [
      branchEdge("s1", "x", "cond-1", "branch-a"),
      branchEdge("s1", "y", "cond-1", "branch-b"),
      branchEdge("s2", "z", "cond-2", "branch-a"),
    ];
    const scopes = collectConditionalBranchScopes(nodes, edges);
    const keys = scopes.map(getScopeKey);
    expect(new Set(keys).size).toBe(keys.length);
  });

  test("top-level and loop edges contribute no branch scopes", () => {
    const nodes: Array<SortableScopeNode> = [
      start("top-start"),
      loop("loop-1"),
      block("a"),
      adder("top-adder"),
    ];
    const edges: Array<Edge> = [
      edge("top-start", "a"),
      edge("a", "loop-1"),
      edge("loop-1", "top-adder"),
    ];
    expect(collectConditionalBranchScopes(nodes, edges)).toEqual([]);
  });

  test("empty conditionals (no branch-tagged edges) contribute no scopes", () => {
    // A conditional node exists but no edges reference it — can happen
    // transiently between data fetches or for a conditional with zero
    // branches. The collector must not fabricate a scope in that case.
    const nodes: Array<SortableScopeNode> = [
      conditional("cond-1"),
      start("cond-start", "cond-1"),
      adder("cond-adder", "cond-1"),
    ];
    const edges: Array<Edge> = [];
    expect(collectConditionalBranchScopes(nodes, edges)).toEqual([]);
  });

  test("branch-tagged edges pointing at unknown conditional ids are ignored", () => {
    // Defensive: if a stale edge references a removed conditional node, the
    // collector should not emit a scope for a node that no longer exists.
    const nodes: Array<SortableScopeNode> = [conditional("cond-1")];
    const edges: Array<Edge> = [
      branchEdge("s", "x", "cond-1", "branch-a"),
      branchEdge("s", "y", "gone-cond", "branch-a"),
    ];
    const scopes = collectConditionalBranchScopes(nodes, edges);
    expect(scopes).toEqual([
      { parentId: "cond-1", conditionalBranchId: "branch-a" },
    ]);
  });
});
