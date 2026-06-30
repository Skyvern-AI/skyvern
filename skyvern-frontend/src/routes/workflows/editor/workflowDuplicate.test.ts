import type { Edge } from "@xyflow/react";
import { describe, expect, it } from "vitest";

import type { AppNode } from "./nodes";
import { duplicateBlockBelow } from "./workflowDuplicate";

function idGenerator(prefix = "new") {
  let count = 0;
  return () => `${prefix}-${++count}`;
}

function labelGenerator(existingLabels: Array<string>) {
  const existing = new Set(existingLabels);
  let index = 1;
  while (existing.has(`block_${index}`)) {
    index += 1;
  }
  return `block_${index}`;
}

function block(
  id: string,
  label: string,
  overrides: Omit<Partial<AppNode>, "data"> & {
    data?: Record<string, unknown>;
  } = {},
): AppNode {
  return {
    id,
    type: "navigation",
    position: { x: 0, y: 0 },
    data: {
      debuggable: true,
      editable: true,
      label,
      continueOnFailure: false,
      model: null,
      parameterKeys: [],
      prompt: "",
      url: "",
      navigationGoal: "",
      errorCodeMapping: "null",
      allowDownloads: false,
      downloadSuffix: null,
      maxRetries: null,
      maxStepsOverride: null,
      totpIdentifier: null,
      totpVerificationUrl: null,
      disableCache: false,
      completeCriterion: "",
      terminateCriterion: "",
      engine: null,
      legacyV2Available: false,
      includeActionHistoryInVerification: false,
      maxSteps: 10,
      ...((overrides.data as Record<string, unknown> | undefined) ?? {}),
    },
    ...overrides,
  } as AppNode;
}

function edge(
  id: string,
  source: string,
  target: string,
  overrides: Partial<Edge> = {},
): Edge {
  return {
    id,
    source,
    target,
    type: "edgeWithAddButton",
    ...overrides,
  };
}

describe("duplicateBlockBelow", () => {
  it("copies a block below itself and rewires the execution edge", () => {
    const nodes = [block("a", "block_1"), block("b", "block_2")];
    const edges = [edge("a-b", "a", "b")];

    const result = duplicateBlockBelow({
      nodes,
      edges,
      nodeId: "a",
      generateId: idGenerator(),
      generateLabel: labelGenerator,
    });

    expect(result?.duplicatedNodeId).toBe("new-1");
    expect(result?.duplicatedLabel).toBe("block_3");
    expect(result?.nodes.map((node) => node.id)).toEqual(["a", "new-1", "b"]);
    expect(result?.nodes.find((node) => node.id === "new-1")?.data).toEqual(
      expect.objectContaining({
        label: "block_3",
        navigationGoal: "",
      }),
    );
    expect(result?.edges).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ source: "a", target: "new-1" }),
        expect.objectContaining({ source: "new-1", target: "b" }),
      ]),
    );
    expect(result?.edges.some((candidate) => candidate.id === "a-b")).toBe(
      false,
    );
  });

  it("preserves inactive conditional branch edges when duplicating within the active branch", () => {
    const nodes = [
      block("conditional", "block_1", {
        type: "conditional",
        data: {
          debuggable: true,
          editable: true,
          label: "block_1",
          continueOnFailure: false,
          model: null,
          branches: [
            {
              id: "branch-a",
              criteria: null,
              description: null,
              is_default: false,
              next_block_label: null,
            },
            {
              id: "branch-b",
              criteria: null,
              description: null,
              is_default: true,
              next_block_label: null,
            },
          ],
          activeBranchId: "branch-a",
          mergeLabel: null,
        },
      }),
      block("a", "block_2", {
        parentId: "conditional",
        data: {
          debuggable: true,
          editable: true,
          label: "block_2",
          continueOnFailure: false,
          model: null,
          conditionalBranchId: "branch-a",
          conditionalLabel: "block_1",
          conditionalMergeLabel: null,
          conditionalNodeId: "conditional",
        },
      }),
      block("next", "block_3", { parentId: "conditional" }),
    ];
    const edges = [
      edge("a-next-active", "a", "next", {
        data: {
          conditionalBranchId: "branch-a",
          conditionalNodeId: "conditional",
        },
      }),
      edge("a-next-inactive", "a", "next", {
        data: {
          conditionalBranchId: "branch-b",
          conditionalNodeId: "conditional",
        },
      }),
    ];

    const result = duplicateBlockBelow({
      nodes,
      edges,
      nodeId: "a",
      generateId: idGenerator(),
      generateLabel: labelGenerator,
    });

    expect(result?.edges).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ id: "a-next-inactive" }),
        expect.objectContaining({
          source: "a",
          target: "new-1",
          data: {
            conditionalBranchId: "branch-a",
            conditionalNodeId: "conditional",
          },
        }),
        expect.objectContaining({
          source: "new-1",
          target: "next",
          data: {
            conditionalBranchId: "branch-a",
            conditionalNodeId: "conditional",
          },
        }),
      ]),
    );
    expect(result?.edges.find((edge) => edge.source === "a")).toEqual(
      expect.objectContaining({
        target: "new-1",
        data: {
          conditionalBranchId: "branch-a",
          conditionalNodeId: "conditional",
        },
      }),
    );
    expect(
      result?.edges.some((candidate) => candidate.id === "a-next-active"),
    ).toBe(false);
  });

  it("duplicates container descendants, remaps branch ids, and rewrites internal output references", () => {
    const nodes = [
      block("conditional", "block_1", {
        type: "conditional",
        data: {
          debuggable: true,
          editable: true,
          label: "block_1",
          continueOnFailure: false,
          model: null,
          branches: [
            {
              id: "branch-a",
              criteria: null,
              description: null,
              is_default: false,
              next_block_label: null,
            },
            {
              id: "branch-default",
              criteria: null,
              description: null,
              is_default: true,
              next_block_label: null,
            },
          ],
          activeBranchId: "branch-a",
          mergeLabel: null,
        },
      }),
      {
        id: "start",
        type: "start",
        parentId: "conditional",
        position: { x: 0, y: 0 },
        data: {
          withWorkflowSettings: false,
          editable: true,
          label: "__start_block__",
          showCode: false,
          parentNodeType: "conditional",
        },
      } as AppNode,
      block("child", "block_2", {
        hidden: false,
        parentId: "conditional",
        data: {
          debuggable: true,
          editable: true,
          label: "block_2",
          continueOnFailure: false,
          model: null,
          conditionalBranchId: "branch-a",
          conditionalLabel: "block_1",
          conditionalMergeLabel: null,
          conditionalNodeId: "conditional",
          navigationGoal: "Use {{ block_1_output }}",
          parameterKeys: ["block_1_output"],
        },
      }),
      {
        id: "adder",
        type: "nodeAdder",
        parentId: "conditional",
        position: { x: 0, y: 0 },
        data: {},
      } as AppNode,
      block("after", "block_3"),
    ];
    const edges = [
      edge("conditional-after", "conditional", "after"),
      edge("start-child", "start", "child", {
        data: {
          conditionalBranchId: "branch-a",
          conditionalNodeId: "conditional",
        },
      }),
      edge("child-adder", "child", "adder", {
        type: "default",
        data: {
          conditionalBranchId: "branch-a",
          conditionalNodeId: "conditional",
        },
      }),
    ];

    const result = duplicateBlockBelow({
      nodes,
      edges,
      nodeId: "conditional",
      generateId: idGenerator(),
      generateLabel: labelGenerator,
    });

    const clonedConditional = result?.nodes.find((node) => node.id === "new-1");
    const clonedStart = result?.nodes.find((node) => node.id === "new-2");
    const clonedChild = result?.nodes.find((node) => node.id === "new-3");
    const clonedAdder = result?.nodes.find((node) => node.id === "new-4");

    expect(clonedConditional?.data.label).toBe("block_4");
    expect(clonedStart?.parentId).toBe("new-1");
    expect(clonedChild?.parentId).toBe("new-1");
    expect(clonedChild?.hidden).toBe(false);
    expect(clonedAdder?.parentId).toBe("new-1");
    expect(clonedChild?.data).toEqual(
      expect.objectContaining({
        conditionalBranchId: "new-5",
        conditionalLabel: "block_4",
        conditionalNodeId: "new-1",
        label: "block_5",
        navigationGoal: "Use {{ block_4_output }}",
        parameterKeys: ["block_4_output"],
      }),
    );
    expect(clonedConditional?.data).toEqual(
      expect.objectContaining({
        activeBranchId: "new-5",
        branches: expect.arrayContaining([
          expect.objectContaining({ id: "new-5" }),
          expect.objectContaining({ id: "new-6" }),
        ]),
      }),
    );
    expect(result?.edges).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ source: "conditional", target: "new-1" }),
        expect.objectContaining({ source: "new-1", target: "after" }),
        expect.objectContaining({
          source: "new-2",
          target: "new-3",
          data: {
            conditionalBranchId: "new-5",
            conditionalNodeId: "new-1",
          },
        }),
        expect.objectContaining({
          source: "new-3",
          target: "new-4",
          data: {
            conditionalBranchId: "new-5",
            conditionalNodeId: "new-1",
          },
        }),
      ]),
    );
  });

  it("rewrites cloned code block references to cloned descendant outputs", () => {
    const nodes = [
      block("conditional", "block_1", {
        type: "conditional",
        data: {
          debuggable: true,
          editable: true,
          label: "block_1",
          continueOnFailure: false,
          model: null,
          branches: [
            {
              id: "branch-a",
              criteria: null,
              description: null,
              is_default: true,
              next_block_label: null,
            },
          ],
          activeBranchId: "branch-a",
          mergeLabel: null,
        },
      }),
      block("dependency", "block_2", {
        parentId: "conditional",
        data: {
          debuggable: true,
          editable: true,
          label: "block_2",
          continueOnFailure: false,
          model: null,
          conditionalBranchId: "branch-a",
          conditionalLabel: "block_1",
          conditionalMergeLabel: null,
          conditionalNodeId: "conditional",
        },
      }),
      block("code", "block_3", {
        type: "codeBlock",
        parentId: "conditional",
        data: {
          debuggable: true,
          editable: true,
          label: "block_3",
          continueOnFailure: false,
          model: null,
          conditionalBranchId: "branch-a",
          conditionalLabel: "block_1",
          conditionalMergeLabel: null,
          conditionalNodeId: "conditional",
          code:
            "result = block_2_output\n" +
            "other = block_20_output\n" +
            "prefixed = prefix_block_2_output\n" +
            "nested = data['block_2_output']",
          prompt: "Use block_2_output and {{ block_2_output }}",
          parameterKeys: ["block_2_output"],
        },
      }),
    ];
    const edges = [
      edge("conditional-dependency", "conditional", "dependency"),
      edge("dependency-code", "dependency", "code"),
    ];

    const result = duplicateBlockBelow({
      nodes,
      edges,
      nodeId: "conditional",
      generateId: idGenerator(),
      generateLabel: labelGenerator,
    });

    const clonedCode = result?.nodes.find((node) => node.id === "new-3");

    expect(clonedCode?.data).toEqual(
      expect.objectContaining({
        code:
          "result = block_5_output\n" +
          "other = block_20_output\n" +
          "prefixed = prefix_block_2_output\n" +
          "nested = data['block_5_output']",
        label: "block_6",
        parameterKeys: ["block_5_output"],
        prompt: "Use block_5_output and {{ block_5_output }}",
      }),
    );
  });

  it("preserves inactive conditional branch visibility in cloned descendants", () => {
    const nodes = [
      block("conditional", "block_1", {
        type: "conditional",
        data: {
          debuggable: true,
          editable: true,
          label: "block_1",
          continueOnFailure: false,
          model: null,
          branches: [
            {
              id: "branch-a",
              criteria: null,
              description: null,
              is_default: false,
              next_block_label: null,
            },
            {
              id: "branch-b",
              criteria: null,
              description: null,
              is_default: true,
              next_block_label: null,
            },
          ],
          activeBranchId: "branch-a",
          mergeLabel: null,
        },
      }),
      block("active-child", "block_2", {
        hidden: false,
        parentId: "conditional",
        data: {
          debuggable: true,
          editable: true,
          label: "block_2",
          continueOnFailure: false,
          model: null,
          conditionalBranchId: "branch-a",
          conditionalLabel: "block_1",
          conditionalMergeLabel: null,
          conditionalNodeId: "conditional",
        },
      }),
      block("inactive-child", "block_3", {
        hidden: true,
        parentId: "conditional",
        data: {
          debuggable: true,
          editable: true,
          label: "block_3",
          continueOnFailure: false,
          model: null,
          conditionalBranchId: "branch-b",
          conditionalLabel: "block_1",
          conditionalMergeLabel: null,
          conditionalNodeId: "conditional",
        },
      }),
      block("after", "block_4"),
    ];
    const edges = [
      edge("conditional-after", "conditional", "after"),
      edge("conditional-active", "conditional", "active-child", {
        hidden: false,
        data: {
          conditionalBranchId: "branch-a",
          conditionalNodeId: "conditional",
        },
      }),
      edge("conditional-inactive", "conditional", "inactive-child", {
        hidden: true,
        data: {
          conditionalBranchId: "branch-b",
          conditionalNodeId: "conditional",
        },
      }),
    ];

    const result = duplicateBlockBelow({
      nodes,
      edges,
      nodeId: "conditional",
      generateId: idGenerator(),
      generateLabel: labelGenerator,
    });

    const clonedActive = result?.nodes.find((node) => node.id === "new-2");
    const clonedInactive = result?.nodes.find((node) => node.id === "new-3");
    const clonedInactiveEdge = result?.edges.find(
      (edge) => edge.source === "new-1" && edge.target === "new-3",
    );

    expect(clonedActive?.hidden).toBe(false);
    expect(clonedInactive?.hidden).toBe(true);
    expect(clonedInactive?.data).toEqual(
      expect.objectContaining({
        conditionalBranchId: "new-5",
        conditionalNodeId: "new-1",
      }),
    );
    expect(clonedInactiveEdge).toEqual(
      expect.objectContaining({
        hidden: true,
        data: expect.objectContaining({
          conditionalBranchId: "new-5",
          conditionalNodeId: "new-1",
        }),
      }),
    );
  });
});
