import { describe, expect, test } from "vitest";

import {
  applyDescendantCollapseVisibility,
  replayPersistedCollapseVisibility,
} from "./applyDescendantCollapseVisibility";
import type { AppNode } from "../nodes";

function n(
  id: string,
  type: string,
  label: string,
  parentId?: string,
  extra?: { data?: Record<string, unknown>; hidden?: boolean },
): AppNode {
  const node = {
    id,
    type,
    parentId,
    position: { x: 0, y: 0 },
    data: { label, ...(extra?.data ?? {}) },
    hidden: extra?.hidden,
  };
  return node as unknown as AppNode;
}

describe("applyDescendantCollapseVisibility", () => {
  test("collapsing a for_loop hides its direct children", () => {
    const nodes = [
      n("loop1", "loop", "loop_a"),
      n("start1", "start", "__start__", "loop1"),
      n("adder1", "nodeAdder", "__adder__", "loop1"),
    ];
    const result = applyDescendantCollapseVisibility(
      nodes,
      "loop1",
      true,
      () => false,
    );
    expect(result.find((x) => x.id === "loop1")?.hidden).toBeFalsy();
    expect(result.find((x) => x.id === "start1")?.hidden).toBe(true);
    expect(result.find((x) => x.id === "adder1")?.hidden).toBe(true);
  });

  test("collapsing a for_loop hides nested conditional's grandchildren", () => {
    const nodes = [
      n("loop1", "loop", "loop_a"),
      n("start1", "start", "__start_loop__", "loop1"),
      n("adder1", "nodeAdder", "__adder_loop__", "loop1"),
      n("cond1", "conditional", "cond_a", "loop1", {
        data: { label: "cond_a", activeBranchId: "branch1" },
      }),
      n("startC", "start", "__start_cond__", "cond1"),
      n("adderC", "nodeAdder", "__adder_cond__", "cond1"),
      n("task1", "task", "task_a", "cond1", {
        data: { label: "task_a", conditionalBranchId: "branch1" },
      }),
    ];
    const result = applyDescendantCollapseVisibility(
      nodes,
      "loop1",
      true,
      () => false,
    );
    expect(result.find((x) => x.id === "cond1")?.hidden).toBe(true);
    expect(result.find((x) => x.id === "startC")?.hidden).toBe(true);
    expect(result.find((x) => x.id === "adderC")?.hidden).toBe(true);
    expect(result.find((x) => x.id === "task1")?.hidden).toBe(true);
  });

  test("expanding for_loop unhides everything when no inner collapse", () => {
    const nodes = [
      n("loop1", "loop", "loop_a"),
      n("cond1", "conditional", "cond_a", "loop1", {
        data: { label: "cond_a", activeBranchId: "branch1" },
      }),
      n("task1", "task", "task_a", "cond1", {
        data: { label: "task_a", conditionalBranchId: "branch1" },
        hidden: true,
      }),
    ];
    const result = applyDescendantCollapseVisibility(
      nodes,
      "loop1",
      false,
      () => false,
    );
    expect(result.find((x) => x.id === "cond1")?.hidden).toBe(false);
    expect(result.find((x) => x.id === "task1")?.hidden).toBe(false);
  });

  test("expanding for_loop keeps inner-collapsed-conditional's children hidden", () => {
    const nodes = [
      n("loop1", "loop", "loop_a"),
      n("cond1", "conditional", "cond_a", "loop1", {
        data: { label: "cond_a", activeBranchId: "branch1" },
      }),
      n("task1", "task", "task_a", "cond1", {
        data: { label: "task_a", conditionalBranchId: "branch1" },
        hidden: true,
      }),
    ];
    const result = applyDescendantCollapseVisibility(
      nodes,
      "loop1",
      false,
      (label) => label === "cond_a",
    );
    expect(result.find((x) => x.id === "cond1")?.hidden).toBe(false);
    expect(result.find((x) => x.id === "task1")?.hidden).toBe(true);
  });

  test("expanding hides task whose branch is inactive", () => {
    const nodes = [
      n("cond1", "conditional", "cond_a", undefined, {
        data: { label: "cond_a", activeBranchId: "branch1" },
      }),
      n("taskActive", "task", "task_a", "cond1", {
        data: { label: "task_a", conditionalBranchId: "branch1" },
      }),
      n("taskInactive", "task", "task_b", "cond1", {
        data: { label: "task_b", conditionalBranchId: "branch2" },
      }),
    ];
    const result = applyDescendantCollapseVisibility(
      nodes,
      "cond1",
      false,
      () => false,
    );
    expect(result.find((x) => x.id === "taskActive")?.hidden).toBe(false);
    expect(result.find((x) => x.id === "taskInactive")?.hidden).toBe(true);
  });

  test("nodes outside the rootId subtree are untouched", () => {
    const nodes = [
      n("loop1", "loop", "loop_a"),
      n("startInside", "start", "__start_a__", "loop1", { hidden: false }),
      n("loop2", "loop", "loop_b"),
      n("startOutside", "start", "__start_b__", "loop2", { hidden: false }),
    ];
    const result = applyDescendantCollapseVisibility(
      nodes,
      "loop1",
      true,
      () => false,
    );
    expect(result.find((x) => x.id === "startInside")?.hidden).toBe(true);
    expect(result.find((x) => x.id === "loop2")?.hidden).toBeFalsy();
    expect(result.find((x) => x.id === "startOutside")?.hidden).toBeFalsy();
  });

  test("expanding cascades hidden state down a 3-level deep collapsed chain", () => {
    const nodes = [
      n("loop1", "loop", "loop_a"),
      n("loopInner", "loop", "loop_inner", "loop1"),
      n("cond1", "conditional", "cond_a", "loopInner", {
        data: { label: "cond_a", activeBranchId: "branch1" },
      }),
      n("task1", "task", "task_a", "cond1", {
        data: { label: "task_a", conditionalBranchId: "branch1" },
      }),
    ];
    const result = applyDescendantCollapseVisibility(
      nodes,
      "loop1",
      false,
      (label) => label === "loop_inner",
    );
    expect(result.find((x) => x.id === "loopInner")?.hidden).toBe(false);
    expect(result.find((x) => x.id === "cond1")?.hidden).toBe(true);
    expect(result.find((x) => x.id === "task1")?.hidden).toBe(true);
  });

  test("sentinels (no conditionalBranchId) inside conditional stay visible", () => {
    const nodes = [
      n("cond1", "conditional", "cond_a", undefined, {
        data: { label: "cond_a", activeBranchId: "branch1" },
      }),
      n("startC", "start", "__start__", "cond1"),
      n("adderC", "nodeAdder", "__adder__", "cond1"),
    ];
    const result = applyDescendantCollapseVisibility(
      nodes,
      "cond1",
      false,
      () => false,
    );
    expect(result.find((x) => x.id === "startC")?.hidden).toBe(false);
    expect(result.find((x) => x.id === "adderC")?.hidden).toBe(false);
  });
});

describe("replayPersistedCollapseVisibility", () => {
  test("hides descendants of every block that is persisted as collapsed", () => {
    const nodes = [
      n("loop1", "loop", "loop_a"),
      n("cond1", "conditional", "cond_a", "loop1", {
        data: { label: "cond_a", activeBranchId: "branchA" },
      }),
      n("task1", "task", "task_a", "cond1", {
        data: { label: "task_a", conditionalBranchId: "branchA" },
      }),
    ];
    const result = replayPersistedCollapseVisibility(nodes, "wf_x", {
      "wf_x\x1floop_a": true,
    });
    expect(result.find((x) => x.id === "cond1")?.hidden).toBe(true);
    expect(result.find((x) => x.id === "task1")?.hidden).toBe(true);
  });

  test("nested-collapsed inner block hides only its own descendants when outer is expanded", () => {
    const nodes = [
      n("cond1", "conditional", "outer", undefined, {
        data: { label: "outer", activeBranchId: "branchA" },
      }),
      n("cond2", "conditional", "inner", "cond1", {
        data: {
          label: "inner",
          activeBranchId: "innerA",
          conditionalBranchId: "branchA",
        },
      }),
      n("task1", "task", "leaf", "cond2", {
        data: { label: "leaf", conditionalBranchId: "innerA" },
      }),
    ];
    const result = replayPersistedCollapseVisibility(nodes, "wf_x", {
      "wf_x\x1finner": true,
    });
    expect(result.find((x) => x.id === "cond1")?.hidden).toBeFalsy();
    expect(result.find((x) => x.id === "cond2")?.hidden).toBeFalsy();
    expect(result.find((x) => x.id === "task1")?.hidden).toBe(true);
  });

  test("returns nodes unchanged when no persisted collapse entries match", () => {
    const nodes = [
      n("loop1", "loop", "loop_a"),
      n("task1", "task", "task_a", "loop1"),
    ];
    const result = replayPersistedCollapseVisibility(nodes, "wf_x", {});
    expect(result).toEqual(nodes);
  });

  test("ignores entries persisted under a different workflow id", () => {
    const nodes = [
      n("loop1", "loop", "loop_a"),
      n("task1", "task", "task_a", "loop1"),
    ];
    const result = replayPersistedCollapseVisibility(nodes, "wf_x", {
      "wf_other\x1floop_a": true,
    });
    expect(result.find((x) => x.id === "task1")?.hidden).toBeFalsy();
  });
});
