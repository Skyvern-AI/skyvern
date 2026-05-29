import { describe, expect, test } from "vitest";

import { ProxyLocation } from "@/api/types";

import type {
  CodeBlock,
  ConditionalBlock,
  OutputParameter,
  WorkflowBlock,
  WorkflowSettings,
} from "../types/workflowTypes";

import {
  getElements,
  getWorkflowBlocks,
  validateWorkflowBlocks,
} from "./workflowEditorUtils";

const DEFAULT_SETTINGS: WorkflowSettings = {
  proxyLocation: ProxyLocation.Residential,
  webhookCallbackUrl: null,
  persistBrowserSession: false,
  browserProfileId: null,
  model: null,
  maxScreenshotScrolls: null,
  extraHttpHeaders: null,
  cdpConnectHeaders: null,
  runWith: "code",
  codeVersion: 2,
  scriptCacheKey: null,
  aiFallback: true,
  runSequentially: false,
  sequentialKey: null,
  finallyBlockLabel: null,
  workflowSystemPrompt: null,
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
});
