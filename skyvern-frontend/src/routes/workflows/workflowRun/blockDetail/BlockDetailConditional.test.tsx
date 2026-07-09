// @vitest-environment jsdom

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Status } from "@/api/types";
import type { WorkflowRunBlock } from "../../types/workflowRunTypes";
import { BlockDetailConditional } from "./BlockDetailConditional";

function buildConditional(
  overrides: Partial<WorkflowRunBlock> = {},
): WorkflowRunBlock {
  return {
    workflow_run_block_id: "wrb_cond",
    workflow_run_id: "wr_default",
    parent_workflow_run_block_id: null,
    block_type: "conditional",
    label: "validate_npiexist",
    description: null,
    title: null,
    status: Status.Completed,
    failure_reason: null,
    output: null,
    continue_on_failure: false,
    task_id: null,
    url: null,
    navigation_goal: null,
    navigation_payload: null,
    data_extraction_goal: null,
    data_schema: null,
    terminate_criterion: null,
    complete_criterion: null,
    include_action_history_in_verification: null,
    engine: null,
    actions: null,
    created_at: "2026-01-01T00:00:00Z",
    modified_at: "2026-01-01T00:00:00Z",
    duration: null,
    loop_values: null,
    current_value: null,
    current_index: null,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
});

describe("BlockDetailConditional", () => {
  it("renders each evaluation's expression, rendered form, and matched indicator", () => {
    const block = buildConditional({
      executed_branch_id: "b_default",
      output: {
        evaluations: [
          {
            branch_id: "b_true",
            branch_index: 0,
            criteria_type: "jinja2_template",
            original_expression: "{{ result == true }}",
            rendered_expression: "false == true",
            result: false,
            is_matched: false,
            is_default: false,
            next_block_label: "next_true",
            error: null,
          },
          {
            branch_id: "b_default",
            branch_index: 1,
            criteria_type: null,
            original_expression: null,
            rendered_expression: null,
            result: null,
            is_matched: true,
            is_default: true,
            next_block_label: "fallback",
            error: null,
          },
        ],
      },
    });

    render(<BlockDetailConditional block={block} />);
    // The unmatched branch's expression renders as-is
    expect(screen.getByText("{{ result == true }}")).toBeDefined();
    // The rendered expression renders separately when it differs
    expect(screen.getByText("false == true")).toBeDefined();
    // The default branch label is announced for branches without an expression
    expect(screen.getByText(/default branch/i)).toBeDefined();
    // The matched branch shows the next block target
    expect(screen.getByText("fallback")).toBeDefined();
  });

  it("falls back to the legacy executed_branch_expression rendering when no evaluations array", () => {
    const block = buildConditional({
      executed_branch_id: "b_match",
      executed_branch_expression: "{{ x == 1 }}",
      executed_branch_result: true,
    });

    render(<BlockDetailConditional block={block} />);
    expect(screen.getByText(/evaluation/i)).toBeDefined();
    expect(screen.getByText("{{ x == 1 }}")).toBeDefined();
  });

  it("renders valid JSON rendered branch values with the JSON explorer", () => {
    const block = buildConditional({
      executed_branch_id: "b_json",
      output: {
        evaluations: [
          {
            branch_id: "b_json",
            branch_index: 0,
            criteria_type: "jinja2_template",
            original_expression: "{{ response }}",
            rendered_expression:
              '{"status_code":200,"response_headers":{"X-Stage":"signin"}}',
            result: true,
            is_matched: true,
            is_default: false,
            next_block_label: "next_block",
            error: null,
          },
        ],
      },
    });

    render(<BlockDetailConditional block={block} />);

    expect(screen.getByText("rendered")).toBeDefined();
    expect(screen.getByText("status_code")).toBeDefined();
    expect(screen.getByText("200")).toBeDefined();
    expect(screen.getByText(/X-Stage.*signin/)).toBeDefined();
    expect(screen.queryByText(/Object\(\d+\)/)).toBeNull();
    expect(screen.getByPlaceholderText("Search JSON")).toBeDefined();
  });

  it("renders a clear message when the default branch executed and no expression matched", () => {
    const block = buildConditional({
      executed_branch_id: "b_default",
      executed_branch_expression: null,
      executed_branch_result: null,
    });
    render(<BlockDetailConditional block={block} />);
    expect(screen.getByText(/no conditions matched/i)).toBeDefined();
  });

  it("renders no evaluation/branches section before the conditional has resolved a branch", () => {
    const block = buildConditional({
      status: Status.Running,
      executed_branch_id: null,
      executed_branch_expression: null,
      executed_branch_result: null,
    });
    render(<BlockDetailConditional block={block} />);
    // Both the "Branches" and "Evaluation" sections should stay hidden
    expect(screen.queryByText(/branches/i)).toBeNull();
    expect(screen.queryByText(/^evaluation/i)).toBeNull();
    expect(screen.queryByText(/no conditions matched/i)).toBeNull();
  });
});
