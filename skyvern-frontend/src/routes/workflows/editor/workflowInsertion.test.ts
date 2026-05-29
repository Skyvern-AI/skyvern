import type { Edge } from "@xyflow/react";
import { describe, expect, it } from "vitest";

import type { BranchContext } from "@/store/WorkflowPanelStore";

import {
  findBranchContextForInsertion,
  shouldKeepExistingEdgeForInsertion,
} from "./workflowInsertion";

const branch: BranchContext = {
  branchId: "branch-a",
  conditionalLabel: "conditional",
  conditionalNodeId: "conditional-node",
  mergeLabel: null,
};

function edge(overrides: Partial<Edge>): Edge {
  return {
    id: "edge",
    source: "previous",
    target: "next",
    ...overrides,
  };
}

describe("shouldKeepExistingEdgeForInsertion", () => {
  it("removes the selected outgoing edge for non-branch insertions", () => {
    expect(
      shouldKeepExistingEdgeForInsertion(edge({}), {
        previous: "previous",
        next: "next",
      }),
    ).toBe(false);
  });

  it("keeps unrelated outgoing edges from the same previous node", () => {
    expect(
      shouldKeepExistingEdgeForInsertion(edge({ target: "other-next" }), {
        previous: "previous",
        next: "next",
      }),
    ).toBe(true);
  });

  it("removes the selected edge for the active conditional branch", () => {
    expect(
      shouldKeepExistingEdgeForInsertion(
        edge({ data: { conditionalBranchId: "branch-a" } }),
        {
          branch,
          previous: "previous",
          next: "next",
        },
      ),
    ).toBe(false);
  });

  it("keeps selected edges for other conditional branches", () => {
    expect(
      shouldKeepExistingEdgeForInsertion(
        edge({ data: { conditionalBranchId: "branch-b" } }),
        {
          branch,
          previous: "previous",
          next: "next",
        },
      ),
    ).toBe(true);
  });

  it("removes untagged selected edges when branch context comes from a nested parent", () => {
    expect(
      shouldKeepExistingEdgeForInsertion(edge({ data: undefined }), {
        branch,
        previous: "previous",
        next: "next",
      }),
    ).toBe(false);
  });
});

describe("findBranchContextForInsertion", () => {
  it("uses branch metadata from the insertion node", () => {
    expect(
      findBranchContextForInsertion(
        [
          {
            id: "previous",
            data: {
              conditionalBranchId: "branch-a",
              conditionalLabel: "conditional",
              conditionalMergeLabel: null,
              conditionalNodeId: "conditional-node",
              label: "block_1",
            },
          },
        ],
        "previous",
      ),
    ).toEqual(branch);
  });

  it("climbs to a parent loop when an empty nested loop inserts from its start node", () => {
    expect(
      findBranchContextForInsertion(
        [
          {
            id: "loop",
            parentId: "conditional-node",
            type: "loop",
            data: {
              conditionalBranchId: "branch-a",
              conditionalLabel: "conditional",
              conditionalMergeLabel: null,
              conditionalNodeId: "conditional-node",
              label: "block_8",
            },
          },
          {
            id: "start",
            parentId: "loop",
            type: "start",
            data: {},
          },
        ],
        "start",
        "loop",
      ),
    ).toEqual(branch);
  });

  it("uses the active branch from a direct conditional parent", () => {
    expect(
      findBranchContextForInsertion(
        [
          {
            id: "conditional-node",
            type: "conditional",
            data: {
              activeBranchId: "branch-a",
              branches: [{ id: "branch-a" }, { id: "branch-b" }],
              label: "conditional",
              mergeLabel: null,
            },
          },
          {
            id: "start",
            parentId: "conditional-node",
            type: "start",
            data: {},
          },
        ],
        "start",
      ),
    ).toEqual(branch);
  });

  it("does not branch-scope insertion after a top-level conditional node", () => {
    expect(
      findBranchContextForInsertion(
        [
          {
            id: "conditional-node",
            type: "conditional",
            data: {
              activeBranchId: "branch-a",
              branches: [{ id: "branch-a" }, { id: "branch-b" }],
              label: "conditional",
              mergeLabel: null,
            },
          },
        ],
        "conditional-node",
      ),
    ).toBeUndefined();
  });

  it("uses the active branch when the caller only has a conditional parent", () => {
    expect(
      findBranchContextForInsertion(
        [
          {
            id: "conditional-node",
            type: "conditional",
            data: {
              activeBranchId: "branch-a",
              branches: [{ id: "branch-a" }, { id: "branch-b" }],
              label: "conditional",
              mergeLabel: null,
            },
          },
        ],
        undefined,
        "conditional-node",
      ),
    ).toEqual(branch);
  });

  it("uses the inner conditional's active branch when inserting into a nested conditional", () => {
    // SKY-10460: a conditional nested inside another conditional carries its
    // own membership metadata (conditionalNodeId/conditionalBranchId pointing
    // at the OUTER conditional). Inserting into the inner conditional's empty
    // branch starts from its START node and walks up to the inner conditional;
    // the context must be the inner conditional's active branch, not its
    // membership in the outer conditional, or the inserted block is orphaned.
    expect(
      findBranchContextForInsertion(
        [
          {
            id: "outer-conditional",
            type: "conditional",
            data: {
              activeBranchId: "outer-branch-a",
              branches: [{ id: "outer-branch-a" }, { id: "outer-branch-b" }],
              label: "outer",
              mergeLabel: null,
            },
          },
          {
            id: "inner-conditional",
            parentId: "outer-conditional",
            type: "conditional",
            data: {
              // Membership in the OUTER conditional.
              conditionalNodeId: "outer-conditional",
              conditionalBranchId: "outer-branch-a",
              conditionalLabel: "outer",
              conditionalMergeLabel: null,
              // The inner conditional's own branch state.
              activeBranchId: "inner-branch-a",
              branches: [{ id: "inner-branch-a" }, { id: "inner-branch-b" }],
              label: "inner",
              mergeLabel: null,
            },
          },
          {
            id: "inner-start",
            parentId: "inner-conditional",
            type: "start",
            data: {},
          },
        ],
        "inner-start",
        "inner-conditional",
      ),
    ).toEqual({
      branchId: "inner-branch-a",
      conditionalLabel: "inner",
      conditionalNodeId: "inner-conditional",
      mergeLabel: null,
    });
  });
});
