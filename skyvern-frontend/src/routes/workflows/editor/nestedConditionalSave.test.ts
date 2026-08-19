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
import { rewireBlockDropInScope } from "./sortable/rewire";
import { TOP_LEVEL_SCOPE } from "./sortable/scope";
import {
  getElements,
  getWorkflowBlocks,
  getWorkflowSettings,
  validateWorkflowBlocks,
} from "./workflowEditorUtils";

const DEFAULT_SETTINGS: WorkflowSettings = {
  proxyLocation: ProxyLocation.Residential,
  webhookCallbackUrl: null,
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
  enableSelfHealing: false,
  maskSecrets: false,
  runSequentially: false,
  sequentialKey: null,
  finallyBlockLabel: null,
  workflowSystemPrompt: null,
  errorCodeMapping: null,
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
