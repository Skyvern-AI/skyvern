import { describe, expect, test } from "vitest";

import type {
  CodeBlock,
  ConditionalBlock,
  ForLoopBlock,
  OutputParameter,
  WorkflowBlock,
} from "../../types/workflowTypes";
import {
  applySequentialDefaulting,
  findChainRoot,
  referencedLabels,
  validateWorkflowBlocks,
  WorkflowValidationError,
} from "../workflowEditorUtils";

function op(label: string): OutputParameter {
  return {
    parameter_type: "output",
    key: `${label}_output`,
    description: null,
    output_parameter_id: `op-${label}`,
    workflow_id: "wf-fixture",
    created_at: "2026-05-12T00:00:00Z",
    modified_at: "2026-05-12T00:00:00Z",
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

describe("referencedLabels", () => {
  test("collects next_block_label across the array", () => {
    const blocks: Array<WorkflowBlock> = [
      code("B1", "B2"),
      code("B2", "B3"),
      code("B3", null),
    ];
    expect(referencedLabels(blocks)).toEqual(new Set(["B2", "B3"]));
  });

  test("includes branch_conditions[].next_block_label from conditionals", () => {
    const cond: ConditionalBlock = {
      label: "C1",
      block_type: "conditional",
      continue_on_failure: false,
      model: null,
      next_block_label: "M",
      output_parameter: op("C1"),
      branch_conditions: [
        {
          id: "br-a",
          description: "a",
          next_block_label: "A1",
          criteria: null,
          is_default: false,
        },
        {
          id: "br-b",
          description: "b",
          next_block_label: "B1",
          criteria: null,
          is_default: true,
        },
      ],
    };
    const blocks: Array<WorkflowBlock> = [
      cond,
      code("A1", "M"),
      code("B1", "M"),
      code("M", null),
    ];
    expect(referencedLabels(blocks)).toEqual(new Set(["M", "A1", "B1"]));
  });

  test("ignores null targets", () => {
    expect(referencedLabels([code("B1", null)])).toEqual(new Set());
  });
});

describe("findChainRoot", () => {
  test("returns the single unreferenced block", () => {
    const blocks: Array<WorkflowBlock> = [
      code("B1", "B2"),
      code("B2", "B3"),
      code("B3", null),
    ];
    expect(findChainRoot(blocks)?.label).toBe("B1");
  });

  test("order-invariant: same root regardless of array permutation", () => {
    const blocks: Array<WorkflowBlock> = [
      code("B3", "B1"),
      code("B1", "B2"),
      code("B2", "B4"),
      code("B4", null),
    ];
    const shuffled = [blocks[2]!, blocks[0]!, blocks[3]!, blocks[1]!];
    expect(findChainRoot(blocks)?.label).toBe("B3");
    expect(findChainRoot(shuffled)?.label).toBe("B3");
  });

  test("returns null when there are zero roots (cycle)", () => {
    const blocks: Array<WorkflowBlock> = [code("B1", "B2"), code("B2", "B1")];
    expect(findChainRoot(blocks)).toBeNull();
  });

  test("returns null when there are multiple roots (disconnected)", () => {
    const blocks: Array<WorkflowBlock> = [code("B1", null), code("B2", null)];
    expect(findChainRoot(blocks)).toBeNull();
  });

  test("returns null on empty input", () => {
    expect(findChainRoot([])).toBeNull();
  });
});

describe("applySequentialDefaulting", () => {
  test("v1 chain with all null next_block_label gets sequential defaults", () => {
    const blocks: Array<WorkflowBlock> = [
      code("B1", null),
      code("B2", null),
      code("B3", null),
    ];
    const result = applySequentialDefaulting(blocks);
    expect(result.map((b) => [b.label, b.next_block_label])).toEqual([
      ["B1", "B2"],
      ["B2", "B3"],
      ["B3", null],
    ]);
  });

  test("v2 chain (all explicit) is unchanged", () => {
    const blocks: Array<WorkflowBlock> = [
      code("B1", "B2"),
      code("B2", "B3"),
      code("B3", null),
    ];
    const result = applySequentialDefaulting(blocks);
    expect(result.map((b) => [b.label, b.next_block_label])).toEqual([
      ["B1", "B2"],
      ["B2", "B3"],
      ["B3", null],
    ]);
  });

  test("does NOT default when any block at this level is conditional", () => {
    const cond: ConditionalBlock = {
      label: "C1",
      block_type: "conditional",
      continue_on_failure: false,
      model: null,
      next_block_label: null,
      output_parameter: op("C1"),
      branch_conditions: [
        {
          id: "br-a",
          description: "a",
          next_block_label: null,
          criteria: null,
          is_default: true,
        },
      ],
    };
    const blocks: Array<WorkflowBlock> = [
      code("B1", null),
      cond,
      code("B2", null),
    ];
    const result = applySequentialDefaulting(blocks);
    expect(result.map((b) => b.next_block_label)).toEqual([null, null, null]);
  });

  test("recurses into loop_blocks at each nesting level", () => {
    const inner: Array<WorkflowBlock> = [code("L1", null), code("L2", null)];
    const loop: ForLoopBlock = {
      label: "FOR",
      block_type: "for_loop",
      continue_on_failure: false,
      model: null,
      next_block_label: null,
      output_parameter: op("FOR"),
      loop_over: { key: "items" } as never,
      loop_blocks: inner,
      loop_variable_reference: null,
      complete_if_empty: false,
      data_schema: null,
    };
    const result = applySequentialDefaulting([loop, code("AFTER", null)]);
    expect(result[0]!.next_block_label).toBe("AFTER");
    expect(result[1]!.next_block_label).toBeNull();
    const looped = result[0] as ForLoopBlock;
    expect(looped.loop_blocks.map((b) => b.next_block_label)).toEqual([
      "L2",
      null,
    ]);
  });

  test("idempotent — running twice produces the same result", () => {
    const blocks: Array<WorkflowBlock> = [code("B1", null), code("B2", null)];
    const once = applySequentialDefaulting(blocks);
    const twice = applySequentialDefaulting(once);
    expect(twice).toEqual(once);
  });
});

describe("validateWorkflowBlocks", () => {
  test("accepts a well-formed v2 chain", () => {
    expect(() =>
      validateWorkflowBlocks([code("B1", "B2"), code("B2", null)]),
    ).not.toThrow();
  });

  test("rejects duplicate labels", () => {
    expect(() =>
      validateWorkflowBlocks([code("B1", "B1"), code("B1", null)]),
    ).toThrow(WorkflowValidationError);
    expect(() =>
      validateWorkflowBlocks([code("B1", "B1"), code("B1", null)]),
    ).toThrow(/Duplicate block label/);
  });

  test("rejects dangling next_block_label", () => {
    expect(() =>
      validateWorkflowBlocks([code("B1", "DOES_NOT_EXIST")]),
    ).toThrow(/references unknown next_block_label/);
  });

  test("rejects zero roots (full cycle)", () => {
    expect(() =>
      validateWorkflowBlocks([code("B1", "B2"), code("B2", "B1")]),
    ).toThrow(/Circular reference detected/);
  });

  test("rejects multiple roots (disconnected)", () => {
    expect(() =>
      validateWorkflowBlocks([code("B1", null), code("B2", null)]),
    ).toThrow(/Disconnected blocks detected/);
  });

  test("rejects partial cycle (one root + cycle elsewhere)", () => {
    expect(() =>
      validateWorkflowBlocks([
        code("B1", "B2"),
        code("B2", "B3"),
        code("B3", "B2"),
      ]),
    ).toThrow(/Circular reference|infinite cycle/);
  });

  test("recurses into loop_blocks and reports the failing nesting level", () => {
    const innerBad: Array<WorkflowBlock> = [code("L1", "L2"), code("L2", "L1")];
    const loop: ForLoopBlock = {
      label: "FOR1",
      block_type: "for_loop",
      continue_on_failure: false,
      model: null,
      next_block_label: null,
      output_parameter: op("FOR1"),
      loop_over: { key: "items" } as never,
      loop_blocks: innerBad,
      loop_variable_reference: null,
      complete_if_empty: false,
      data_schema: null,
    };
    expect(() => validateWorkflowBlocks([loop])).toThrow(
      /Circular reference.+inside loop FOR1|inside loop FOR1.+cycle/,
    );
  });
});
