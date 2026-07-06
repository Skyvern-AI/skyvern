// @vitest-environment jsdom

import { afterEach, describe, expect, test, vi } from "vitest";

import type { AppNode } from "../editor/nodes";
import { START_ANCHOR_TOP_PX } from "../editor/paneFit";
import {
  BLOCK_JUMP_DURATION_MS,
  BRANCH_SETTLE_STABLE_FRAMES,
  BRANCH_SETTLE_TIMEOUT_MS,
  blockJumpDuration,
  collectBlockSearchTargets,
  filterBlockSearchTargets,
  focusBlockTarget,
  resolveConditionalBranchPath,
  waitForNodeSettle,
  type FocusBlockDeps,
} from "./blockSearch";

const startNode = {
  id: "start-node",
  type: "start",
  position: { x: 0, y: 0 },
  data: { label: "__start_block__" },
} as AppNode;

const adderNode = {
  id: "adder-node",
  type: "nodeAdder",
  position: { x: 0, y: 900 },
  data: {},
} as AppNode;

const loginNode = {
  id: "login-node",
  type: "task",
  position: { x: 0, y: 100 },
  measured: { width: 400, height: 200 },
  data: { label: "Login" },
} as AppNode;

const loopNode = {
  id: "loop-node",
  type: "loop",
  position: { x: 0, y: 400 },
  data: { label: "Iterate rows", loopKind: "while" },
} as AppNode;

// Nested inside the loop container: `position` is parent-relative.
const nestedNode = {
  id: "nested-node",
  type: "extraction",
  parentId: "loop-node",
  position: { x: 40, y: 80 },
  data: { label: "Extract row data" },
} as AppNode;

const unlabeledNode = {
  id: "unlabeled-node",
  type: "task",
  position: { x: 0, y: 700 },
  data: { label: "   " },
} as AppNode;

const nodes = [
  startNode,
  loginNode,
  loopNode,
  nestedNode,
  unlabeledNode,
  adderNode,
];

// A conditional flow: branch A active, branch B hidden. Branch B holds a task
// plus a loop with a cascaded (affinity-less) child, and a nested conditional
// whose branch Y holds the deepest target.
function makeConditionalNodes(): Array<AppNode> {
  return [
    startNode,
    {
      id: "cond-node",
      type: "conditional",
      position: { x: 0, y: 100 },
      data: { label: "Route by type", activeBranchId: "branch-a" },
    } as AppNode,
    {
      id: "branch-a-block",
      type: "task",
      parentId: "cond-node",
      position: { x: 20, y: 120 },
      data: {
        label: "Handle type A",
        conditionalNodeId: "cond-node",
        conditionalBranchId: "branch-a",
      },
    } as AppNode,
    {
      id: "branch-b-block",
      type: "task",
      parentId: "cond-node",
      position: { x: 20, y: 120 },
      hidden: true,
      data: {
        label: "Handle type B",
        conditionalNodeId: "cond-node",
        conditionalBranchId: "branch-b",
      },
    } as AppNode,
    {
      id: "branch-b-loop",
      type: "loop",
      parentId: "cond-node",
      position: { x: 20, y: 360 },
      hidden: true,
      data: {
        label: "Iterate type B rows",
        conditionalNodeId: "cond-node",
        conditionalBranchId: "branch-b",
      },
    } as AppNode,
    {
      id: "branch-b-loop-child",
      type: "extraction",
      parentId: "branch-b-loop",
      position: { x: 30, y: 60 },
      hidden: true,
      data: { label: "Extract type B row" },
    } as AppNode,
    {
      id: "inner-cond-node",
      type: "conditional",
      parentId: "cond-node",
      position: { x: 20, y: 600 },
      hidden: true,
      data: {
        label: "Route type B result",
        activeBranchId: "branch-x",
        conditionalNodeId: "cond-node",
        conditionalBranchId: "branch-b",
      },
    } as AppNode,
    {
      id: "inner-branch-y-block",
      type: "sendEmail",
      parentId: "inner-cond-node",
      position: { x: 10, y: 80 },
      hidden: true,
      data: {
        label: "Email type B result",
        conditionalNodeId: "inner-cond-node",
        conditionalBranchId: "branch-y",
      },
    } as AppNode,
  ];
}

describe("collectBlockSearchTargets", () => {
  test("keeps workflow blocks (including nested ones) in canvas order and maps block types", () => {
    expect(collectBlockSearchTargets(nodes)).toEqual([
      { nodeId: "login-node", label: "Login", blockType: "task" },
      { nodeId: "loop-node", label: "Iterate rows", blockType: "while_loop" },
      {
        nodeId: "nested-node",
        label: "Extract row data",
        blockType: "extraction",
      },
    ]);
  });

  test("skips utility nodes and blank labels", () => {
    const targets = collectBlockSearchTargets(nodes);
    const ids = targets.map((target) => target.nodeId);
    expect(ids).not.toContain("start-node");
    expect(ids).not.toContain("adder-node");
    expect(ids).not.toContain("unlabeled-node");
  });

  test("includes blocks hidden inside inactive conditional branches", () => {
    const ids = collectBlockSearchTargets(makeConditionalNodes()).map(
      (target) => target.nodeId,
    );
    expect(ids).toContain("branch-b-block");
    expect(ids).toContain("branch-b-loop-child");
    expect(ids).toContain("inner-branch-y-block");
  });
});

describe("filterBlockSearchTargets", () => {
  const targets = collectBlockSearchTargets(nodes);

  test("returns every target for an empty or whitespace query", () => {
    expect(filterBlockSearchTargets(targets, "")).toEqual(targets);
    expect(filterBlockSearchTargets(targets, "   ")).toEqual(targets);
  });

  test("matches case-insensitive substrings", () => {
    expect(
      filterBlockSearchTargets(targets, "ROW").map((t) => t.nodeId),
    ).toEqual(["loop-node", "nested-node"]);
    expect(
      filterBlockSearchTargets(targets, "login").map((t) => t.nodeId),
    ).toEqual(["login-node"]);
  });

  test("trims the query before matching", () => {
    expect(
      filterBlockSearchTargets(targets, "  login  ").map((t) => t.nodeId),
    ).toEqual(["login-node"]);
  });

  test("returns nothing when no label matches", () => {
    expect(filterBlockSearchTargets(targets, "does-not-exist")).toEqual([]);
  });
});

describe("blockJumpDuration", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  test("is instant when matchMedia is unavailable (jsdom default)", () => {
    expect(blockJumpDuration()).toBe(0);
  });

  test("is instant under prefers-reduced-motion", () => {
    vi.stubGlobal("matchMedia", () => ({ matches: true }));
    expect(blockJumpDuration()).toBe(0);
  });

  test("animates otherwise", () => {
    vi.stubGlobal("matchMedia", () => ({ matches: false }));
    expect(blockJumpDuration()).toBe(BLOCK_JUMP_DURATION_MS);
  });
});

describe("resolveConditionalBranchPath", () => {
  test("is empty for targets outside conditionals or in the active branch", () => {
    expect(resolveConditionalBranchPath(nodes, "nested-node")).toEqual([]);
    expect(
      resolveConditionalBranchPath(makeConditionalNodes(), "branch-a-block"),
    ).toEqual([]);
  });

  test("resolves a direct child of an inactive branch", () => {
    expect(
      resolveConditionalBranchPath(makeConditionalNodes(), "branch-b-block"),
    ).toEqual([
      {
        conditionalId: "cond-node",
        conditionalLabel: "Route by type",
        branchId: "branch-b",
      },
    ]);
  });

  test("reaches the branch chain through parentId nesting (block in a loop)", () => {
    expect(
      resolveConditionalBranchPath(
        makeConditionalNodes(),
        "branch-b-loop-child",
      ),
    ).toEqual([
      {
        conditionalId: "cond-node",
        conditionalLabel: "Route by type",
        branchId: "branch-b",
      },
    ]);
  });

  test("orders nested conditional switches root→leaf", () => {
    expect(
      resolveConditionalBranchPath(
        makeConditionalNodes(),
        "inner-branch-y-block",
      ),
    ).toEqual([
      {
        conditionalId: "cond-node",
        conditionalLabel: "Route by type",
        branchId: "branch-b",
      },
      {
        conditionalId: "inner-cond-node",
        conditionalLabel: "Route type B result",
        branchId: "branch-y",
      },
    ]);
  });

  test("skips levels whose branch is already active", () => {
    const alreadySwitched = makeConditionalNodes().map((node) =>
      node.id === "cond-node"
        ? ({
            ...node,
            data: { ...node.data, activeBranchId: "branch-b" },
          } as AppNode)
        : node,
    );
    expect(
      resolveConditionalBranchPath(alreadySwitched, "inner-branch-y-block"),
    ).toEqual([
      {
        conditionalId: "inner-cond-node",
        conditionalLabel: "Route type B result",
        branchId: "branch-y",
      },
    ]);
  });

  test("stops at a dangling conditional reference", () => {
    const orphan = {
      id: "orphan-node",
      type: "task",
      position: { x: 0, y: 0 },
      data: {
        label: "Orphan",
        conditionalNodeId: "gone-node",
        conditionalBranchId: "branch-z",
      },
    } as AppNode;
    expect(resolveConditionalBranchPath([orphan], "orphan-node")).toEqual([]);
  });
});

describe("waitForNodeSettle", () => {
  function makeFrameQueue() {
    const queue: Array<() => void> = [];
    return {
      requestFrame: (callback: () => void) => {
        queue.push(callback);
      },
      async tick() {
        queue.splice(0).forEach((callback) => callback());
        await Promise.resolve();
      },
    };
  }

  test("resolves once the node is visible and its position holds still", async () => {
    const frames = makeFrameQueue();
    const node = {
      id: "n",
      type: "task",
      hidden: true,
      position: { x: 0, y: 0 },
      data: { label: "N" },
    } as AppNode;
    let resolved = false;
    void waitForNodeSettle("n", {
      getNodes: () => [node],
      getInternalNode: () => undefined,
      requestFrame: frames.requestFrame,
      now: () => 0,
    }).then(() => {
      resolved = true;
    });

    await frames.tick(); // hidden
    (node as { hidden?: boolean }).hidden = false;
    node.position = { x: 10, y: 10 };
    await frames.tick(); // visible, but the position just moved
    for (let i = 0; i < BRANCH_SETTLE_STABLE_FRAMES; i++) {
      expect(resolved).toBe(false);
      await frames.tick(); // stable frames accumulate
    }
    expect(resolved).toBe(true);
  });

  test("times out instead of hanging when the node never becomes visible", async () => {
    const frames = makeFrameQueue();
    let clock = 0;
    let resolved = false;
    void waitForNodeSettle("missing", {
      getNodes: () => [],
      getInternalNode: () => undefined,
      requestFrame: frames.requestFrame,
      now: () => {
        clock += BRANCH_SETTLE_TIMEOUT_MS / 2;
        return clock;
      },
    }).then(() => {
      resolved = true;
    });

    await frames.tick();
    await frames.tick();
    expect(resolved).toBe(true);
  });
});

describe("focusBlockTarget", () => {
  function makeDeps(
    depsNodes: Array<AppNode> = nodes,
    overrides?: Partial<FocusBlockDeps>,
  ): FocusBlockDeps {
    return {
      getNodes: () => depsNodes,
      getInternalNode: () => undefined,
      getPaneWidth: () => 1000,
      viewportZoom: 0.75,
      duration: 300,
      setViewport: vi.fn(),
      selectBlock: vi.fn(),
      expandBlock: vi.fn(),
      switchBranch: vi.fn(),
      waitForSettle: vi.fn().mockResolvedValue(undefined),
      ...overrides,
    };
  }

  test("selects the block (feeding the selected-block URL sync) and expands it", async () => {
    const deps = makeDeps();
    await expect(focusBlockTarget("login-node", deps)).resolves.toBe(true);
    expect(deps.selectBlock).toHaveBeenCalledWith("login-node");
    expect(deps.expandBlock).toHaveBeenCalledWith("Login");
  });

  test("visible targets never switch branches or wait", async () => {
    const deps = makeDeps();
    await focusBlockTarget("login-node", deps);
    expect(deps.switchBranch).not.toHaveBeenCalled();
    expect(deps.waitForSettle).not.toHaveBeenCalled();
    expect(deps.selectBlock).toHaveBeenCalledTimes(1);
    expect(deps.setViewport).toHaveBeenCalledTimes(1);
  });

  test("top-anchors the block at its absolute position, horizontally centered, at the current zoom", async () => {
    const deps = makeDeps(nodes, {
      getInternalNode: (nodeId) =>
        nodeId === "nested-node"
          ? {
              internals: { positionAbsolute: { x: 140, y: 480 } },
              measured: { width: 300, height: 120 },
            }
          : undefined,
    });
    await focusBlockTarget("nested-node", deps);
    expect(deps.setViewport).toHaveBeenCalledWith(
      {
        x: 1000 / 2 - (140 + 150) * 0.75,
        y: START_ANCHOR_TOP_PX - 480 * 0.75,
        zoom: 0.75,
      },
      { duration: 300 },
    );
  });

  test("falls back to the node's own position and measurements", async () => {
    const deps = makeDeps();
    await focusBlockTarget("login-node", deps);
    expect(deps.setViewport).toHaveBeenCalledWith(
      {
        x: 1000 / 2 - (0 + 200) * 0.75,
        y: START_ANCHOR_TOP_PX - 100 * 0.75,
        zoom: 0.75,
      },
      { duration: 300 },
    );
  });

  test("refuses unknown ids and utility nodes without side effects", async () => {
    const deps = makeDeps();
    await expect(focusBlockTarget("missing-node", deps)).resolves.toBe(false);
    await expect(focusBlockTarget("start-node", deps)).resolves.toBe(false);
    expect(deps.selectBlock).not.toHaveBeenCalled();
    expect(deps.setViewport).not.toHaveBeenCalled();
    expect(deps.switchBranch).not.toHaveBeenCalled();
  });

  test("hidden-branch target: focuses the conditional, switches the branch, settles, then centers the target on fresh geometry", async () => {
    const conditionalNodes = makeConditionalNodes();
    const calls: Array<string> = [];
    const deps = makeDeps(conditionalNodes, {
      getInternalNode: (nodeId) =>
        nodeId === "cond-node"
          ? {
              internals: { positionAbsolute: { x: 0, y: 100 } },
              measured: { width: 480, height: 300 },
            }
          : undefined,
      selectBlock: vi.fn((id) => calls.push(`select:${id}`)),
      expandBlock: vi.fn((label) => calls.push(`expand:${label}`)),
      setViewport: vi.fn((viewport) =>
        calls.push(`anchor:${viewport.x},${viewport.y}`),
      ),
      switchBranch: vi.fn((conditionalId, branchId) =>
        calls.push(`switch:${conditionalId}:${branchId}`),
      ),
      waitForSettle: vi.fn(async () => {
        calls.push("settle");
        // Simulate the visibility cascade + re-layout moving the target.
        const target = conditionalNodes.find(
          (node) => node.id === "branch-b-block",
        )!;
        (target as { hidden?: boolean }).hidden = false;
        target.position = { x: 500, y: 700 };
        (target as { measured?: { width: number; height: number } }).measured =
          { width: 400, height: 100 };
      }),
    });

    await expect(focusBlockTarget("branch-b-block", deps)).resolves.toBe(true);
    // Conditional: internal (0,100) 480 wide -> x=500-240*0.75, y=24-100*0.75.
    // Target post-settle (500,700) 400 wide -> x=500-700*0.75, y=24-700*0.75.
    expect(calls).toEqual([
      "select:cond-node",
      "expand:Route by type",
      `anchor:320,${START_ANCHOR_TOP_PX - 75}`,
      "switch:cond-node:branch-b",
      "settle",
      "select:branch-b-block",
      "expand:Handle type B",
      // Post-settle geometry, not the stale pre-switch position.
      `anchor:-25,${START_ANCHOR_TOP_PX - 525}`,
    ]);
  });

  test("nested conditionals switch every level root→leaf with a settle between", async () => {
    const conditionalNodes = makeConditionalNodes();
    const calls: Array<string> = [];
    const deps = makeDeps(conditionalNodes, {
      selectBlock: vi.fn((id) => calls.push(`select:${id}`)),
      switchBranch: vi.fn((conditionalId, branchId) =>
        calls.push(`switch:${conditionalId}:${branchId}`),
      ),
      waitForSettle: vi.fn(async () => {
        calls.push("settle");
      }),
    });

    await expect(focusBlockTarget("inner-branch-y-block", deps)).resolves.toBe(
      true,
    );
    expect(
      calls.filter(
        (entry) => entry.startsWith("switch:") || entry === "settle",
      ),
    ).toEqual([
      "switch:cond-node:branch-b",
      "settle",
      "switch:inner-cond-node:branch-y",
      "settle",
    ]);
    expect(calls[calls.length - 1]).toBe("select:inner-branch-y-block");
  });
});
