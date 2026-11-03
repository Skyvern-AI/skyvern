import { describe, it, expect } from "vitest";

import type { AppNode } from "../nodes";
import { resolveBlockLocation } from "./resolveBlockPath";

function node(
  id: string,
  type: string,
  data: Record<string, unknown>,
  parentId?: string,
): AppNode {
  return {
    id,
    type,
    parentId,
    position: { x: 0, y: 0 },
    data,
  } as unknown as AppNode;
}

function byId(nodes: Array<AppNode>): Map<string, AppNode> {
  return new Map(nodes.map((n) => [n.id, n]));
}

describe("resolveBlockLocation", () => {
  it("returns an empty location for a top-level block", () => {
    const nodes = [node("n1", "login", { label: "block_1" })];
    expect(resolveBlockLocation(byId(nodes), "n1")).toEqual({
      ancestorIds: [],
      path: [],
    });
  });

  it("collects a loop ancestor", () => {
    const nodes = [
      node("loop1", "loop", { label: "loop_1" }),
      node("n1", "login", { label: "block_2" }, "loop1"),
    ];
    expect(resolveBlockLocation(byId(nodes), "n1")).toEqual({
      ancestorIds: ["loop1"],
      path: [{ kind: "loop", label: "loop_1" }],
    });
  });

  it("labels the default conditional branch as 'else'", () => {
    const nodes = [
      node("c1", "conditional", {
        label: "cond_1",
        branches: [
          { id: "b1", is_default: false, description: "ok" },
          { id: "b2", is_default: true },
        ],
      }),
      node(
        "n1",
        "login",
        { label: "block_3", conditionalBranchId: "b2" },
        "c1",
      ),
    ];
    expect(resolveBlockLocation(byId(nodes), "n1").path).toEqual([
      { kind: "conditional", label: "cond_1", branch: "else" },
    ]);
  });

  it("falls back to a positional branch label when unnamed", () => {
    const nodes = [
      node("c1", "conditional", {
        label: "cond_1",
        branches: [
          { id: "b1", is_default: false },
          { id: "b2", is_default: true },
        ],
      }),
      node(
        "n1",
        "login",
        { label: "block_3", conditionalBranchId: "b1" },
        "c1",
      ),
    ];
    expect(resolveBlockLocation(byId(nodes), "n1").path).toEqual([
      { kind: "conditional", label: "cond_1", branch: "branch 1" },
    ]);
  });

  it("skips non-container ancestors", () => {
    const nodes = [
      node("task1", "task", { label: "task_1" }),
      node("n1", "login", { label: "block_2" }, "task1"),
    ];
    expect(resolveBlockLocation(byId(nodes), "n1")).toEqual({
      ancestorIds: [],
      path: [],
    });
  });

  it("does not loop forever on a self-referential parent", () => {
    const nodes = [node("n1", "login", { label: "block_1" }, "n1")];
    expect(resolveBlockLocation(byId(nodes), "n1")).toEqual({
      ancestorIds: [],
      path: [],
    });
  });

  it("records each ancestor once when the parent chain cycles", () => {
    const nodes = [
      node("loopA", "loop", { label: "loop_a" }, "loopB"),
      node("loopB", "loop", { label: "loop_b" }, "loopA"),
      node("n1", "login", { label: "block_1" }, "loopA"),
    ];
    expect(resolveBlockLocation(byId(nodes), "n1")).toEqual({
      ancestorIds: ["loopA", "loopB"],
      path: [
        { kind: "loop", label: "loop_b" },
        { kind: "loop", label: "loop_a" },
      ],
    });
  });
});
