import { describe, it, expect } from "vitest";
import type { Edge } from "@xyflow/react";
import { applyEdgeVisibility } from "./applyEdgeVisibility";

const COND_ID = "cond-1";
const BRANCH_A = "branch-a";
const BRANCH_B = "branch-b";

type VisNode = {
  id: string;
  hidden?: boolean;
  type?: string;
  data?: Record<string, unknown>;
};

function edge(
  overrides: Partial<Edge> & { source: string; target: string },
): Edge {
  return {
    id: "e1",
    type: "edgeWithAddButton",
    ...overrides,
  };
}

function toMap(nodes: VisNode[]): Map<string, VisNode> {
  return new Map(nodes.map((n) => [n.id, n]));
}

const EMPTY = new Map<string, VisNode>();

describe("applyEdgeVisibility", () => {
  it("hides edge whose conditionalBranchId differs from active branch", () => {
    const e = edge({
      source: "s",
      target: "t",
      data: { conditionalNodeId: COND_ID, conditionalBranchId: BRANCH_B },
    });
    const result = applyEdgeVisibility(e, EMPTY, COND_ID, BRANCH_A);
    expect(result.hidden).toBe(true);
  });

  it("shows edge whose conditionalBranchId matches active branch", () => {
    const e = edge({
      source: "s",
      target: "t",
      data: { conditionalNodeId: COND_ID, conditionalBranchId: BRANCH_A },
    });
    const result = applyEdgeVisibility(e, EMPTY, COND_ID, BRANCH_A);
    expect(result.hidden).toBe(false);
  });

  it("hides edge when source node is hidden", () => {
    const e = edge({ source: "s", target: "t" });
    const nodes = toMap([
      { id: "s", hidden: true, data: {} },
      { id: "t", hidden: false, data: {} },
    ]);
    const result = applyEdgeVisibility(e, nodes, COND_ID, BRANCH_A);
    expect(result.hidden).toBe(true);
  });

  it("unhides loop START edge when both nodes visible and no branch affinity", () => {
    const e = edge({
      source: "loop-start",
      target: "loop-adder",
      hidden: true,
    });
    const nodes = toMap([
      { id: "loop-start", hidden: false, data: {} },
      { id: "loop-adder", hidden: false, data: {} },
    ]);
    const result = applyEdgeVisibility(e, nodes, COND_ID, BRANCH_A);
    expect(result.hidden).toBe(false);
  });

  it("does NOT unhide edge whose target belongs to inactive branch", () => {
    const e = edge({ source: "start", target: "block-b", hidden: true });
    const nodes = toMap([
      { id: "start", hidden: false, type: "start", data: {} },
      {
        id: "block-b",
        hidden: false,
        data: {
          conditionalNodeId: COND_ID,
          conditionalBranchId: BRANCH_B,
        },
      },
    ]);
    const result = applyEdgeVisibility(e, nodes, COND_ID, BRANCH_A);
    expect(result.hidden).toBe(true);
  });

  it("does NOT unhide edge whose source belongs to inactive branch", () => {
    const e = edge({ source: "block-b", target: "adder", hidden: true });
    const nodes = toMap([
      {
        id: "block-b",
        hidden: false,
        data: {
          conditionalNodeId: COND_ID,
          conditionalBranchId: BRANCH_B,
        },
      },
      { id: "adder", hidden: false, type: "nodeAdder", data: {} },
    ]);
    const result = applyEdgeVisibility(e, nodes, COND_ID, BRANCH_A);
    expect(result.hidden).toBe(true);
  });

  it("ignores branch affinity from a different conditional", () => {
    const e = edge({ source: "start", target: "block-x", hidden: true });
    const nodes = toMap([
      { id: "start", hidden: false, data: {} },
      {
        id: "block-x",
        hidden: false,
        data: {
          conditionalNodeId: "other-cond",
          conditionalBranchId: BRANCH_B,
        },
      },
    ]);
    const result = applyEdgeVisibility(e, nodes, COND_ID, BRANCH_A);
    expect(result.hidden).toBe(false);
  });

  it("preserves edge unchanged when no rules match", () => {
    const e = edge({ source: "unknown", target: "also-unknown", hidden: true });
    const result = applyEdgeVisibility(e, EMPTY, COND_ID, BRANCH_A);
    expect(result.hidden).toBe(true);
  });
});
