import { describe, expect, test } from "vitest";

import { ProxyLocation } from "@/api/types";

import type {
  CodeBlock,
  ConditionalBlock,
  ForLoopBlock,
  OutputParameter,
  WorkflowBlock,
  WorkflowSettings,
} from "../types/workflowTypes";

import {
  type AppNode,
  isWorkflowBlockNode,
  type WorkflowBlockNode,
} from "./nodes";
import {
  createBranchCondition,
  isConditionalNode,
} from "./nodes/ConditionalNode/types";
import { rewireBlockDropInScope } from "./sortable/rewire";
import { duplicateBlockBelow } from "./workflowDuplicate";
import { TOP_LEVEL_SCOPE } from "./sortable/scope";
import {
  getElements,
  getUpdatedNodesAfterLabelUpdateForParameterKeys,
  getWorkflowBlocks,
  getWorkflowSettings,
  layout,
  validateWorkflowBlocks,
} from "./workflowEditorUtils";

const DEFAULT_SETTINGS: WorkflowSettings = {
  proxyLocation: ProxyLocation.Residential,
  webhookCallbackUrl: null,
  totpVerificationUrl: null,
  totpIdentifier: null,
  adaptiveCaching: false,
  generateScriptOnTerminal: false,
  persistBrowserSession: false,
  reuseBrowserSession: false,
  pinSavedSessionIp: false,
  browserProfileId: null,
  browserProfileKey: null,
  model: null,
  maxScreenshotScrolls: null,
  maxElapsedTimeMinutes: null,
  extraHttpHeaders: null,
  cdpConnectHeaders: null,
  runWith: "code",
  codeVersion: 2,
  scriptCacheKey: null,
  aiFallback: true,
  maskSecrets: false,
  runSequentially: false,
  sequentialKey: null,
  finallyBlockLabel: null,
  workflowSystemPrompt: null,
  errorCodeMapping: null,
  retryPolicy: null,
};

function op(label: string): OutputParameter {
  return {
    parameter_type: "output",
    key: `${label}_output`,
    description: null,
    output_parameter_id: `op-${label}`,
    workflow_id: "wf-fixture",
    created_at: "2026-05-28T00:00:00Z",
    modified_at: "2026-05-28T00:00:00Z",
    deleted_at: null,
  };
}

function code(label: string, next: string | null): CodeBlock {
  return {
    label,
    block_type: "code",
    continue_on_failure: false,
    model: null,
    next_block_label: next,
    output_parameter: op(label),
    code: `# ${label}`,
    parameters: [],
    error_code_mapping: null,
  };
}

function conditional(
  label: string,
  mergeNext: string | null,
  branches: Array<{ id: string; next: string | null; isDefault?: boolean }>,
): ConditionalBlock {
  return {
    label,
    block_type: "conditional",
    continue_on_failure: false,
    model: null,
    next_block_label: mergeNext,
    output_parameter: op(label),
    branch_conditions: branches.map((branch) => ({
      id: branch.id,
      description: branch.id,
      next_block_label: branch.next,
      criteria: null,
      is_default: branch.isDefault ?? false,
    })),
  };
}

function forLoop(
  label: string,
  loopBlocks: Array<WorkflowBlock>,
): ForLoopBlock {
  return {
    label,
    block_type: "for_loop",
    continue_on_failure: false,
    model: null,
    next_block_label: null,
    output_parameter: op(label),
    loop_over: { key: "items" } as never,
    loop_blocks: loopBlocks,
    loop_variable_reference: null,
    complete_if_empty: false,
    data_schema: null,
  };
}

const FINALLY_SETTINGS: WorkflowSettings = {
  ...DEFAULT_SETTINGS,
  finallyBlockLabel: "finally_notify",
};

function finallyConditionalBlocks(): Array<WorkflowBlock> {
  return [
    code("start_step", "decide_update"),
    conditional("decide_update", null, [
      { id: "b1", next: "Save_file_api" },
      { id: "b2", next: "unexpected_outcome" },
      { id: "b3", next: "finally_notify" },
      { id: "b4", next: null, isDefault: true },
    ]),
    code("Save_file_api", "finally_notify"),
    code("unexpected_outcome", "finally_notify"),
    code("finally_notify", null),
  ];
}

function getNodeByLabel(
  nodes: Array<AppNode>,
  label: string,
): WorkflowBlockNode {
  const node = nodes.find(
    (candidate): candidate is WorkflowBlockNode =>
      isWorkflowBlockNode(candidate) && candidate.data.label === label,
  );
  if (!node) {
    throw new Error(`Missing workflow node: ${label}`);
  }
  return node;
}

function routingOf(blocks: ReturnType<typeof getWorkflowBlocks>) {
  return blocks.map((block) => ({
    label: block.label,
    nextBlockLabel: block.next_block_label,
    branches:
      block.block_type === "conditional"
        ? block.branch_conditions.map((branch) => ({
            id: branch.id,
            nextBlockLabel: branch.next_block_label,
          }))
        : null,
  }));
}

/**
 * SKY-10460: a conditional nested inside another conditional, with a block
 * inside the inner conditional's branch. The inner branch's next_block_label
 * must point at that block so it stays reachable; otherwise save fails with
 * "Disconnected blocks detected".
 */
describe("nested conditional save round-trip", () => {
  test("inner conditional branch block stays reachable through load -> save", () => {
    const blocks: Array<WorkflowBlock> = [
      conditional("outer", null, [
        { id: "outer-a", next: "inner" },
        { id: "outer-b", next: "block_2", isDefault: true },
      ]),
      conditional("inner", null, [
        { id: "inner-a", next: "block_1" },
        { id: "inner-b", next: null, isDefault: true },
      ]),
      code("block_1", null),
      code("block_2", null),
    ];

    const { nodes, edges } = getElements(blocks, DEFAULT_SETTINGS, true);
    const saved = getWorkflowBlocks(nodes, edges);

    const innerSaved = saved.find((block) => block.label === "inner");
    expect(innerSaved?.block_type).toBe("conditional");
    const innerBranchA = (
      innerSaved as ConditionalBlock
    ).branch_conditions.find((branch) => branch.id === "inner-a");
    expect(innerBranchA?.next_block_label).toBe("block_1");

    expect(() =>
      validateWorkflowBlocks(saved as Array<WorkflowBlock>),
    ).not.toThrow();
  });

  test("reload is robust to the inner block preceding the inner conditional in the array", () => {
    // getWorkflowBlocks appends conditional-branch children in node order, so
    // the persisted array can list block_1 before its owning inner conditional.
    // reconstructConditionalStructure must still attribute block_1 to the inner
    // conditional and produce a connected save.
    const blocks: Array<WorkflowBlock> = [
      conditional("outer", null, [
        { id: "outer-a", next: "inner" },
        { id: "outer-b", next: "block_2", isDefault: true },
      ]),
      code("block_1", null),
      conditional("inner", null, [
        { id: "inner-a", next: "block_1" },
        { id: "inner-b", next: null, isDefault: true },
      ]),
      code("block_2", null),
    ];

    const { nodes, edges } = getElements(blocks, DEFAULT_SETTINGS, true);
    const saved = getWorkflowBlocks(nodes, edges);

    const innerSaved = saved.find((block) => block.label === "inner");
    const innerBranchA = (
      innerSaved as ConditionalBlock
    ).branch_conditions.find((branch) => branch.id === "inner-a");
    expect(innerBranchA?.next_block_label).toBe("block_1");

    expect(() =>
      validateWorkflowBlocks(saved as Array<WorkflowBlock>),
    ).not.toThrow();
  });

  test("finally block stays outside a conditional without a merge point", () => {
    const { nodes } = getElements(
      finallyConditionalBlocks(),
      FINALLY_SETTINGS,
      true,
    );
    const finallyNode = getNodeByLabel(nodes, "finally_notify");

    expect(finallyNode.parentId).toBeUndefined();
    expect(finallyNode.data.conditionalBranchId).toBeNull();
    expect(finallyNode.data.conditionalLabel).toBeNull();
    expect(finallyNode.data.conditionalNodeId).toBeNull();
  });

  test("finally block stays root-owned when reached from a conditional inside a loop", () => {
    const blocks: Array<WorkflowBlock> = [
      forLoop("loop", [
        conditional("inner_decision", null, [
          { id: "inner-a", next: "inner_terminal" },
          { id: "inner-b", next: null, isDefault: true },
        ]),
        code("inner_terminal", "finally_notify"),
      ]),
      code("finally_notify", null),
    ];

    const { nodes } = getElements(blocks, FINALLY_SETTINGS, false);

    expect(getNodeByLabel(nodes, "finally_notify").parentId).toBeUndefined();
  });

  test("a branch pointing directly at the finally block becomes empty", () => {
    const { nodes, edges } = getElements(
      finallyConditionalBlocks(),
      FINALLY_SETTINGS,
      true,
    );
    const conditionalNode = getNodeByLabel(nodes, "decide_update");
    const startNode = nodes.find(
      (node) => node.type === "start" && node.parentId === conditionalNode.id,
    );
    const adderNode = nodes.find(
      (node) =>
        node.type === "nodeAdder" && node.parentId === conditionalNode.id,
    );
    const branchEdge = edges.find(
      (edge) =>
        (edge.data as { conditionalBranchId?: string | null } | undefined)
          ?.conditionalBranchId === "b3",
    );

    expect(startNode).toBeDefined();
    expect(adderNode).toBeDefined();
    expect(branchEdge).toBeDefined();
    expect(branchEdge?.source).toBe(startNode?.id);
    expect(branchEdge?.target).toBe(adderNode?.id);
  });

  test("finally block and setting survive serialization", () => {
    const { nodes, edges } = getElements(
      finallyConditionalBlocks(),
      FINALLY_SETTINGS,
      true,
    );
    const saved = getWorkflowBlocks(nodes, edges);

    expect(saved.some((block) => block.label === "finally_notify")).toBe(true);
    expect(getWorkflowSettings(nodes).finallyBlockLabel).toBe("finally_notify");
  });

  test("a conditional with a merge point keeps the merge outside its branches", () => {
    const blocks: Array<WorkflowBlock> = [
      code("start_step", "decide_update"),
      conditional("decide_update", "merge", [
        { id: "b1", next: "branch_step" },
        { id: "b2", next: "merge", isDefault: true },
      ]),
      code("branch_step", "merge"),
      code("merge", "finally_notify"),
      code("finally_notify", null),
    ];

    const { nodes } = getElements(blocks, FINALLY_SETTINGS, true);
    const conditionalNode = getNodeByLabel(nodes, "decide_update");

    expect(getNodeByLabel(nodes, "branch_step").parentId).toBe(
      conditionalNode.id,
    );
    expect(getNodeByLabel(nodes, "merge").parentId).toBeUndefined();
    expect(getNodeByLabel(nodes, "finally_notify").parentId).toBeUndefined();
  });

  test("branch collection is unchanged without a finally setting", () => {
    const blocks: Array<WorkflowBlock> = [
      code("start_step", "decide_update"),
      conditional("decide_update", null, [
        { id: "b1", next: "ordinary_terminal" },
        { id: "b2", next: null, isDefault: true },
      ]),
      code("ordinary_terminal", null),
    ];

    const { nodes } = getElements(blocks, DEFAULT_SETTINGS, true);
    const conditionalNode = getNodeByLabel(nodes, "decide_update");
    const terminalNode = getNodeByLabel(nodes, "ordinary_terminal");

    expect(terminalNode.parentId).toBe(conditionalNode.id);
    expect(terminalNode.data.conditionalBranchId).toBe("b1");
  });

  test("the trailing adder skips the finally block even when it is listed first", () => {
    const blocks: Array<WorkflowBlock> = [
      code("finally_notify", null),
      code("start_step", "decide_update"),
      conditional("decide_update", null, [
        { id: "b1", next: "Save_file_api" },
        { id: "b2", next: null, isDefault: true },
      ]),
      code("Save_file_api", "finally_notify"),
    ];

    const { nodes, edges } = getElements(blocks, FINALLY_SETTINGS, true);
    const rootAdder = nodes.find(
      (node) => node.type === "nodeAdder" && !node.parentId,
    );
    const conditionalNode = getNodeByLabel(nodes, "decide_update");
    const finallyNode = getNodeByLabel(nodes, "finally_notify");

    // Array position must not decide the chain tail: the finally block is
    // chained after the real main-chain tail, and the adder follows it.
    expect(
      edges.some(
        (edge) =>
          edge.source === conditionalNode.id && edge.target === finallyNode.id,
      ),
    ).toBe(true);
    expect(
      edges.some(
        (edge) =>
          edge.source === finallyNode.id && edge.target === rootAdder?.id,
      ),
    ).toBe(true);
    expect(
      edges.some(
        (edge) =>
          edge.source === conditionalNode.id && edge.target === rootAdder?.id,
      ),
    ).toBe(false);
  });

  test("an orphan finally block is chained last instead of floating", () => {
    const blocks: Array<WorkflowBlock> = [
      code("start_step", "decide_update"),
      conditional("decide_update", "after_step", [
        { id: "b1", next: null },
        { id: "b2", next: null, isDefault: true },
      ]),
      code("after_step", null),
      code("finally_notify", null),
    ];

    const { nodes, edges } = getElements(blocks, FINALLY_SETTINGS, true);
    const finallyNode = getNodeByLabel(nodes, "finally_notify");
    const afterStep = getNodeByLabel(nodes, "after_step");
    const rootAdder = nodes.find(
      (node) => node.type === "nodeAdder" && !node.parentId,
    );

    const inbound = edges.filter((edge) => edge.target === finallyNode.id);
    expect(inbound).toHaveLength(1);
    expect(inbound[0]?.source).toBe(afterStep.id);
    // Must be indistinguishable from a native chain edge.
    expect(inbound[0]?.type).toBe("edgeWithAddButton");
    expect(
      edges.some(
        (edge) =>
          edge.source === finallyNode.id && edge.target === rootAdder?.id,
      ),
    ).toBe(true);
  });

  test("chaining an orphan finally block does not change what is saved", () => {
    const blocks: Array<WorkflowBlock> = [
      code("start_step", "decide_update"),
      conditional("decide_update", "after_step", [
        { id: "b1", next: null },
        { id: "b2", next: null, isDefault: true },
      ]),
      code("after_step", null),
      code("finally_notify", null),
    ];

    const { nodes, edges } = getElements(blocks, FINALLY_SETTINGS, true);
    const saved = getWorkflowBlocks(nodes, edges);
    const routing = Object.fromEntries(
      saved.map((block) => [block.label, block.next_block_label]),
    );

    expect(routing).toEqual({
      start_step: "decide_update",
      decide_update: "after_step",
      after_step: null,
      finally_notify: null,
    });
  });

  test("an orphan finally block is chained even with no conditional present", () => {
    const blocks: Array<WorkflowBlock> = [
      code("step_a", null),
      code("finally_notify", null),
    ];

    const { nodes, edges } = getElements(blocks, FINALLY_SETTINGS, true);
    const finallyNode = getNodeByLabel(nodes, "finally_notify");
    const stepA = getNodeByLabel(nodes, "step_a");

    expect(
      edges.some(
        (edge) => edge.source === stepA.id && edge.target === finallyNode.id,
      ),
    ).toBe(true);
    // Sequential defaulting must no longer materialize that edge on save.
    const saved = getWorkflowBlocks(nodes, edges);
    expect(
      saved.find((block) => block.label === "step_a")?.next_block_label,
    ).toBeNull();
  });

  test("a real edge into the finally block is not duplicated by a synthetic one", () => {
    const blocks: Array<WorkflowBlock> = [
      code("step_a", "step_b"),
      code("step_b", "finally_notify"),
      code("finally_notify", null),
    ];

    const { nodes, edges } = getElements(blocks, FINALLY_SETTINGS, true);
    const finallyNode = getNodeByLabel(nodes, "finally_notify");
    const stepB = getNodeByLabel(nodes, "step_b");

    const inbound = edges.filter((edge) => edge.target === finallyNode.id);
    expect(inbound).toHaveLength(1);
    expect(inbound[0]?.source).toBe(stepB.id);
    const saved = getWorkflowBlocks(nodes, edges);
    expect(
      saved.find((block) => block.label === "step_b")?.next_block_label,
    ).toBe("finally_notify");
  });

  test("reordering the main chain keeps the finally edge display-only", () => {
    const blocks: Array<WorkflowBlock> = [
      code("step_a", null),
      code("step_b", null),
      code("finally_notify", null),
    ];

    const { nodes, edges } = getElements(blocks, FINALLY_SETTINGS, true);
    const stepA = getNodeByLabel(nodes, "step_a");
    const stepB = getNodeByLabel(nodes, "step_b");

    const rewired = rewireBlockDropInScope({
      nodes,
      edges,
      scope: TOP_LEVEL_SCOPE,
      activeId: stepB.id,
      overId: stepA.id,
      finallyBlockId: getNodeByLabel(nodes, "finally_notify").id,
    });
    expect(rewired).not.toBeNull();

    const saved = getWorkflowBlocks(nodes, rewired!.edges);
    const routing = Object.fromEntries(
      saved.map((block) => [block.label, block.next_block_label]),
    );
    // step_b now leads, and the edge into the finally block stays synthetic:
    // the reordered tail must NOT gain a real next_block_label.
    expect(routing.step_b).toBe("step_a");
    expect(routing.step_a).toBeNull();
    expect(routing.finally_notify).toBeNull();
  });

  test("a workflow whose only block is the finally block still connects to START", () => {
    const { nodes, edges } = getElements(
      [code("finally_notify", null)],
      FINALLY_SETTINGS,
      true,
    );
    const rootStart = nodes.find(
      (node) => node.type === "start" && !node.parentId,
    );
    const finallyNode = getNodeByLabel(nodes, "finally_notify");

    expect(
      edges.some(
        (edge) =>
          edge.source === rootStart?.id && edge.target === finallyNode.id,
      ),
    ).toBe(true);
  });

  test("finally routing is stable through save reload and save", () => {
    const firstLoad = getElements(
      finallyConditionalBlocks(),
      FINALLY_SETTINGS,
      true,
    );
    const firstSave = getWorkflowBlocks(firstLoad.nodes, firstLoad.edges);
    const saveFile = firstSave.find((block) => block.label === "Save_file_api");
    const unexpectedOutcome = firstSave.find(
      (block) => block.label === "unexpected_outcome",
    );
    const conditionalBlock = firstSave.find(
      (block) => block.label === "decide_update",
    );

    expect(saveFile?.next_block_label).toBeNull();
    expect(unexpectedOutcome?.next_block_label).toBeNull();
    expect(conditionalBlock?.block_type).toBe("conditional");
    expect(
      conditionalBlock?.block_type === "conditional"
        ? conditionalBlock.branch_conditions.find(
            (branch) => branch.id === "b3",
          )?.next_block_label
        : undefined,
    ).toBeNull();

    const secondLoad = getElements(
      firstSave as Array<WorkflowBlock>,
      FINALLY_SETTINGS,
      true,
    );
    const rootStart = secondLoad.nodes.find(
      (node) => node.type === "start" && !node.parentId,
    );
    const startStep = getNodeByLabel(secondLoad.nodes, "start_step");
    const conditionalNode = getNodeByLabel(secondLoad.nodes, "decide_update");
    const finallyNode = getNodeByLabel(secondLoad.nodes, "finally_notify");

    expect(rootStart).toBeDefined();
    expect(
      secondLoad.edges.some(
        (edge) => edge.source === rootStart?.id && edge.target === startStep.id,
      ),
    ).toBe(true);
    expect(
      secondLoad.edges.some(
        (edge) =>
          edge.source === startStep.id && edge.target === conditionalNode.id,
      ),
    ).toBe(true);
    expect(finallyNode.parentId).toBeUndefined();

    const secondSave = getWorkflowBlocks(secondLoad.nodes, secondLoad.edges);
    expect(routingOf(secondSave)).toEqual(routingOf(firstSave));
  });
});

// Branches that converge on a shared block while the conditional has no merge
// label of its own (a legal API/MCP shape).
describe("conditional without a merge label", () => {
  function savedRoutingSorted(blocks: Array<WorkflowBlock>) {
    const { nodes, edges } = getElements(blocks, DEFAULT_SETTINGS, true);
    return routingOf(getWorkflowBlocks(nodes, edges)).sort((a, b) =>
      a.label.localeCompare(b.label),
    );
  }

  function inputRoutingSorted(blocks: Array<WorkflowBlock>) {
    return routingOf(blocks as never).sort((a, b) =>
      a.label.localeCompare(b.label),
    );
  }

  function branchOf(nodes: Array<AppNode>, label: string) {
    const node = getNodeByLabel(nodes, label);
    return node.parentId ? node.data.conditionalBranchId : null;
  }

  const skipFirst = (): Array<WorkflowBlock> => [
    code("start_step", "decide"),
    conditional("decide", null, [
      { id: "b1", next: "join" },
      { id: "b2", next: "branch_step", isDefault: true },
    ]),
    code("branch_step", "join"),
    code("join", null),
  ];

  const skipLast = (): Array<WorkflowBlock> => [
    code("start_step", "decide"),
    conditional("decide", null, [
      { id: "b1", next: "branch_step" },
      { id: "b2", next: "join", isDefault: true },
    ]),
    code("branch_step", "join"),
    code("join", null),
  ];

  test("a skip branch listed first keeps its target through load -> save", () => {
    expect(savedRoutingSorted(skipFirst())).toEqual(
      inputRoutingSorted(skipFirst()),
    );
  });

  test("the shared block renders after the conditional, not inside a branch", () => {
    for (const blocks of [skipFirst(), skipLast()]) {
      const { nodes } = getElements(blocks, DEFAULT_SETTINGS, true);
      expect(branchOf(nodes, "branch_step")).not.toBeNull();
      expect(branchOf(nodes, "join")).toBeNull();
    }
  });

  test("a skip branch listed last round-trips losslessly", () => {
    expect(savedRoutingSorted(skipLast())).toEqual(
      inputRoutingSorted(skipLast()),
    );
  });

  test("converging branches inside a loop keep their targets", () => {
    const blocks = [forLoop("loop", skipFirst())];
    const { nodes, edges } = getElements(blocks, DEFAULT_SETTINGS, true);
    const saved = getWorkflowBlocks(nodes, edges);
    const loop = saved.find((block) => block.label === "loop");

    expect(branchOf(nodes, "join")).toBeNull();
    expect(
      loop?.block_type === "for_loop"
        ? inputRoutingSorted(loop.loop_blocks as Array<WorkflowBlock>)
        : null,
    ).toEqual(inputRoutingSorted(skipFirst()));
  });

  test("a single branch child with an empty default branch stays in its branch", () => {
    const blocks: Array<WorkflowBlock> = [
      code("start_step", "decide"),
      conditional("decide", null, [
        { id: "b1", next: "branch_step" },
        { id: "b2", next: null, isDefault: true },
      ]),
      code("branch_step", null),
    ];
    const { nodes } = getElements(blocks, DEFAULT_SETTINGS, true);

    expect(branchOf(nodes, "branch_step")).toBe("b1");
    expect(savedRoutingSorted(blocks)).toEqual(inputRoutingSorted(blocks));
  });

  test("a branch targeting the conditional's own next block stays an empty branch", () => {
    const blocks: Array<WorkflowBlock> = [
      code("start_step", "decide"),
      conditional("decide", "after", [
        { id: "b1", next: "after" },
        { id: "b2", next: null, isDefault: true },
      ]),
      code("after", null),
    ];
    const { nodes } = getElements(blocks, DEFAULT_SETTINGS, true);

    expect(branchOf(nodes, "after")).toBeNull();
  });

  test("a nested conditional sharing the outer join round-trips in either order", () => {
    const outer = conditional("outer", null, [
      { id: "o1", next: "inner" },
      { id: "o2", next: "alternate", isDefault: true },
    ]);
    const inner = conditional("inner", null, [
      { id: "i1", next: "inner_step" },
      { id: "i2", next: "outer_join", isDefault: true },
    ]);
    const tail = [
      code("inner_step", "outer_join"),
      code("alternate", "outer_join"),
      code("outer_join", null),
    ];
    for (const blocks of [
      [code("start_step", "outer"), outer, inner, ...tail],
      [code("start_step", "outer"), inner, outer, ...tail],
    ]) {
      const { nodes } = getElements(blocks, DEFAULT_SETTINGS, true);

      expect(branchOf(nodes, "outer_join")).toBeNull();
      expect(savedRoutingSorted(blocks)).toEqual(inputRoutingSorted(blocks));
    }
  });

  test("nested conditionals ending an outer branch keep their merges", () => {
    const explicitInnerMerge = (): Array<WorkflowBlock> => [
      code("start_step", "outer"),
      conditional("outer", null, [
        { id: "o1", next: "inner" },
        { id: "o2", next: "p", isDefault: true },
      ]),
      conditional("inner", "join", [
        { id: "n1", next: "q" },
        { id: "n2", next: "r", isDefault: true },
      ]),
      code("q", "join"),
      code("r", "join"),
      code("p", "join"),
      code("join", null),
    ];
    const outer = conditional("outer", null, [
      { id: "o1", next: "inner" },
      { id: "o2", next: "join", isDefault: true },
    ]);
    const inner = conditional("inner", null, [
      { id: "i1", next: "a" },
      { id: "i2", next: "b", isDefault: true },
    ]);
    const tail = [code("a", "join"), code("b", "join"), code("join", null)];

    for (const blocks of [
      explicitInnerMerge(),
      [code("start_step", "outer"), outer, inner, ...tail],
      [code("start_step", "outer"), inner, outer, ...tail],
    ]) {
      expect(savedRoutingSorted(blocks)).toEqual(inputRoutingSorted(blocks));
    }
  });

  test("an empty Else the editor adds to a conditional without a default saves as null", () => {
    const blocks: Array<WorkflowBlock> = [
      code("start_step", "decide"),
      conditional("decide", null, [
        { id: "b1", next: "join" },
        { id: "b2", next: "branch_step" },
      ]),
      code("branch_step", "join"),
      code("join", null),
    ];
    const { nodes, edges } = getElements(blocks, DEFAULT_SETTINGS, true);
    const decide = nodes.find(isConditionalNode)!;
    decide.data.branches = [
      ...decide.data.branches,
      createBranchCondition({ id: "else", is_default: true }),
    ];
    const saved = getWorkflowBlocks(nodes, edges).find(
      (block) => block.label === "decide",
    );

    expect(
      saved?.block_type === "conditional"
        ? saved.branch_conditions.find((branch) => branch.id === "else")
            ?.next_block_label
        : undefined,
    ).toBeNull();
  });

  test("a conditional with no Else draws the join after it, before and after a save", () => {
    const blocks: Array<WorkflowBlock> = [
      code("start_step", "decide"),
      conditional("decide", null, [
        { id: "b1", next: "join" },
        { id: "b2", next: "child" },
      ]),
      code("child", "join"),
      code("join", null),
    ];
    const first = getElements(blocks, DEFAULT_SETTINGS, true);
    const decide = first.nodes.find(isConditionalNode)!;
    decide.data.branches = [
      ...decide.data.branches,
      createBranchCondition({ id: "else", is_default: true }),
    ];
    const saved = getWorkflowBlocks(first.nodes, first.edges);
    const second = getElements(
      saved as Array<WorkflowBlock>,
      DEFAULT_SETTINGS,
      true,
    );

    for (const { nodes } of [first, second]) {
      expect(branchOf(nodes, "child")).toBe("b2");
      expect(branchOf(nodes, "join")).toBeNull();
    }
    expect(
      routingOf(saved).find((block) => block.label === "decide"),
    ).toMatchObject({
      nextBlockLabel: null,
      branches: [
        { id: "b1", nextBlockLabel: "join" },
        { id: "b2", nextBlockLabel: "child" },
        { id: "else", nextBlockLabel: null },
      ],
    });
    expect(routingOf(getWorkflowBlocks(second.nodes, second.edges))).toEqual(
      routingOf(saved),
    );
  });

  test("the saved block order follows the stored order whichever branch tab is showing", () => {
    const diamond = (): Array<WorkflowBlock> => [
      code("start_step", "decide"),
      conditional("decide", null, [
        { id: "b1", next: "a" },
        { id: "b2", next: "b", isDefault: true },
      ]),
      code("a", "join"),
      code("b", "join"),
      code("join", null),
    ];
    const order = ["start_step", "decide", "a", "b", "join"];

    type Saved = ReturnType<typeof getWorkflowBlocks>;
    const cases: Array<[Array<WorkflowBlock>, (saved: Saved) => Saved]> = [
      [diamond(), (saved) => saved],
      [
        [forLoop("loop", diamond())],
        (saved) =>
          saved[0]?.block_type === "for_loop" ? saved[0].loop_blocks : [],
      ],
    ];
    for (const [blocks, savedOrder] of cases) {
      const { nodes, edges } = getElements(blocks, DEFAULT_SETTINGS, true);
      for (const activeBranch of ["b1", "b2"]) {
        // Layout moves hidden nodes to the end, as a branch tab switch does.
        const shown = layout(
          nodes.map((node) =>
            isWorkflowBlockNode(node) && node.data.conditionalBranchId
              ? {
                  ...node,
                  hidden: node.data.conditionalBranchId !== activeBranch,
                }
              : node,
          ),
          edges,
        );
        const saved = savedOrder(getWorkflowBlocks(shown.nodes, shown.edges));

        expect(saved.map((block) => block.label)).toEqual(order);
      }
    }
  });

  test("a branch that owns no blocks keeps its target when only some branches converge", () => {
    const blocks: Array<WorkflowBlock> = [
      code("start_step", "decide"),
      conditional("decide", null, [
        { id: "b1", next: "join" },
        { id: "b2", next: "branch_step" },
        { id: "b3", next: "other", isDefault: true },
      ]),
      code("branch_step", "join"),
      code("join", null),
      code("other", null),
    ];

    expect(savedRoutingSorted(blocks)).toEqual(inputRoutingSorted(blocks));
  });

  test("two branches sharing a first block keep it when another branch starts at the join", () => {
    const blocks: Array<WorkflowBlock> = [
      code("start_step", "decide"),
      conditional("decide", null, [
        { id: "b1", next: "shared" },
        { id: "b2", next: "shared" },
        { id: "b3", next: "join", isDefault: true },
      ]),
      code("shared", "join"),
      code("join", null),
    ];

    expect(savedRoutingSorted(blocks)).toEqual(inputRoutingSorted(blocks));

    const { nodes, edges } = getElements(blocks, DEFAULT_SETTINGS, true);
    const renamed = getUpdatedNodesAfterLabelUpdateForParameterKeys(
      getNodeByLabel(nodes, "shared").id,
      "renamed",
      nodes,
    ) as Array<AppNode>;
    const decide = getWorkflowBlocks(renamed, edges).find(
      (block) => block.label === "decide",
    );

    expect(
      decide?.block_type === "conditional"
        ? decide.branch_conditions.map((branch) => branch.next_block_label)
        : null,
    ).toEqual(["renamed", "renamed", "join"]);
  });

  test("a duplicated conditional's shared branch target points at its own copy", () => {
    const blocks: Array<WorkflowBlock> = [
      code("start_step", "decide"),
      conditional("decide", null, [
        { id: "b1", next: "shared" },
        { id: "b2", next: "shared" },
        { id: "b3", next: "join", isDefault: true },
      ]),
      code("shared", "join"),
      code("join", null),
    ];
    const { nodes, edges } = getElements(blocks, DEFAULT_SETTINGS, true);
    let nextId = 0;
    let nextLabel = 0;
    const duplicated = duplicateBlockBelow({
      nodes,
      edges,
      nodeId: getNodeByLabel(nodes, "decide").id,
      generateId: () => `dup-${nextId++}`,
      generateLabel: () => `copy_${nextLabel++}`,
    })!;
    const saved = getWorkflowBlocks(duplicated.nodes, duplicated.edges);
    const copy = saved.find(
      (block) => block.label === duplicated.duplicatedLabel,
    );
    const copyTargets =
      copy?.block_type === "conditional"
        ? copy.branch_conditions.map((branch) => branch.next_block_label)
        : [];

    expect(copyTargets[0]).toBe(copyTargets[1]);
    expect(copyTargets).not.toContain("shared");
  });

  test("renaming a nested conditional's merge target keeps the merge", () => {
    const blocks: Array<WorkflowBlock> = [
      code("start_step", "outer"),
      conditional("outer", null, [
        { id: "o1", next: "inner" },
        { id: "o2", next: "p", isDefault: true },
      ]),
      conditional("inner", "join", [
        { id: "n1", next: "q" },
        { id: "n2", next: "r", isDefault: true },
      ]),
      code("q", "join"),
      code("r", "join"),
      code("p", "join"),
      code("join", null),
    ];
    const { nodes, edges } = getElements(blocks, DEFAULT_SETTINGS, true);
    const renamed = getUpdatedNodesAfterLabelUpdateForParameterKeys(
      getNodeByLabel(nodes, "join").id,
      "renamed",
      nodes,
    ) as Array<AppNode>;
    const routing = Object.fromEntries(
      getWorkflowBlocks(renamed, edges).map((block) => [
        block.label,
        block.next_block_label,
      ]),
    );

    expect(routing).toMatchObject({
      inner: "renamed",
      q: "renamed",
      r: "renamed",
    });
  });
});
