import { describe, test, expect } from "vitest";
import { areBlocksIdentical } from "./compareBlocks";
import { WorkflowBlock } from "../types/workflowTypes";

function makeOutputParameter(id: string) {
  return {
    parameter_type: "output" as const,
    key: "out_key",
    description: null,
    output_parameter_id: id,
    workflow_id: `wf-${id}`,
    created_at: "2026-01-01T00:00:00Z",
    modified_at: "2026-01-02T00:00:00Z",
    deleted_at: null,
  };
}

function makeWorkflowParameter(idSuffix: string, key: string) {
  return {
    parameter_type: "workflow" as const,
    key,
    description: null,
    workflow_id: `wf-${idSuffix}`,
    workflow_parameter_id: `wp-${idSuffix}`,
    workflow_parameter_type: "string" as const,
    default_value: null,
    created_at: `2026-01-01T00:00:0${idSuffix}Z`,
    modified_at: `2026-01-02T00:00:0${idSuffix}Z`,
    deleted_at: null,
  };
}

function makeTaskBlock(overrides: Record<string, unknown> = {}): WorkflowBlock {
  return {
    block_type: "task",
    label: "Block 6",
    output_parameter: makeOutputParameter("a"),
    continue_on_failure: false,
    model: null,
    url: null,
    title: "t",
    navigation_goal: "Click submit",
    data_extraction_goal: null,
    data_schema: null,
    complete_criterion: null,
    terminate_criterion: null,
    error_code_mapping: null,
    parameters: [],
    include_action_history_in_verification: false,
    engine: null,
    ...overrides,
  } as unknown as WorkflowBlock;
}

describe("areBlocksIdentical", () => {
  test("identical blocks compare equal", () => {
    expect(areBlocksIdentical(makeTaskBlock(), makeTaskBlock())).toBe(true);
  });

  test("different output_parameter IDs do not flip blocks to modified", () => {
    const a = makeTaskBlock({ output_parameter: makeOutputParameter("a") });
    const b = makeTaskBlock({ output_parameter: makeOutputParameter("b") });
    expect(areBlocksIdentical(a, b)).toBe(true);
  });

  test("different nested workflow_parameter_id values do not flip blocks to modified", () => {
    const a = makeTaskBlock({
      parameters: [makeWorkflowParameter("1", "name")],
    });
    const b = makeTaskBlock({
      parameters: [makeWorkflowParameter("2", "name")],
    });
    expect(areBlocksIdentical(a, b)).toBe(true);
  });

  test("nested ContextParameter.source IDs are stripped", () => {
    const sourceA = makeWorkflowParameter("1", "src");
    const sourceB = makeWorkflowParameter("2", "src");
    const a = makeTaskBlock({
      parameters: [
        {
          parameter_type: "context",
          key: "ctx",
          description: null,
          source: sourceA,
          value: null,
        },
      ],
    });
    const b = makeTaskBlock({
      parameters: [
        {
          parameter_type: "context",
          key: "ctx",
          description: null,
          source: sourceB,
          value: null,
        },
      ],
    });
    expect(areBlocksIdentical(a, b)).toBe(true);
  });

  test("changes to user-visible fields are detected", () => {
    const a = makeTaskBlock({ navigation_goal: "Click submit" });
    const b = makeTaskBlock({ navigation_goal: "Click cancel" });
    expect(areBlocksIdentical(a, b)).toBe(false);
  });

  test("changes to a parameter key are detected", () => {
    const a = makeTaskBlock({
      parameters: [makeWorkflowParameter("1", "old_key")],
    });
    const b = makeTaskBlock({
      parameters: [makeWorkflowParameter("1", "new_key")],
    });
    expect(areBlocksIdentical(a, b)).toBe(false);
  });

  test("changes inside error_code_mapping are detected", () => {
    const a = makeTaskBlock({ error_code_mapping: { e1: "msg-old" } });
    const b = makeTaskBlock({ error_code_mapping: { e1: "msg-new" } });
    expect(areBlocksIdentical(a, b)).toBe(false);
  });

  test("rotated branch_conditions[].id values do not flip conditional blocks to modified", () => {
    const makeConditional = (idSuffix: string): WorkflowBlock =>
      ({
        block_type: "conditional",
        label: "Conditional 1",
        output_parameter: makeOutputParameter(idSuffix),
        continue_on_failure: false,
        model: null,
        branch_conditions: [
          {
            id: `bc-${idSuffix}`,
            criteria: {
              criteria_type: "jinja2_template",
              expression: "{{ x == 1 }}",
              description: null,
            },
            next_block_label: "Block A",
            description: null,
            is_default: false,
          },
        ],
      }) as unknown as WorkflowBlock;

    expect(areBlocksIdentical(makeConditional("1"), makeConditional("2"))).toBe(
      true,
    );
  });

  test("changes to a branch criteria expression are detected", () => {
    const makeConditional = (expression: string): WorkflowBlock =>
      ({
        block_type: "conditional",
        label: "Conditional 1",
        output_parameter: makeOutputParameter("a"),
        continue_on_failure: false,
        model: null,
        branch_conditions: [
          {
            id: "bc-1",
            criteria: {
              criteria_type: "jinja2_template",
              expression,
              description: null,
            },
            next_block_label: "Block A",
            description: null,
            is_default: false,
          },
        ],
      }) as unknown as WorkflowBlock;

    expect(
      areBlocksIdentical(
        makeConditional("{{ x == 1 }}"),
        makeConditional("{{ x == 2 }}"),
      ),
    ).toBe(false);
  });
});
