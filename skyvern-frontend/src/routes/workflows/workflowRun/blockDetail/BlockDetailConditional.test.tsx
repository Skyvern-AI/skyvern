// @vitest-environment jsdom

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Status } from "@/api/types";
import type {
  BranchEvaluation,
  WorkflowRunBlock,
} from "../../types/workflowRunTypes";
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

function evaluation(
  overrides: Partial<BranchEvaluation> & { branch_id: string },
): BranchEvaluation {
  return {
    branch_index: 0,
    criteria_type: "jinja2_template",
    original_expression: null,
    result: null,
    is_matched: false,
    is_default: false,
    next_block_label: null,
    error: null,
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

  it("renders keyword-led rule lines: the branch that ran trails its destination, the rest their result", () => {
    const block = buildConditional({
      executed_branch_id: "b_true",
      output: {
        evaluations: [
          {
            branch_id: "b_true",
            branch_index: 0,
            criteria_type: "jinja2_template",
            original_expression: "{{ count > 3 }}",
            rendered_expression: "5 > 3",
            result: true,
            is_matched: true,
            is_default: false,
            next_block_label: "notify_team",
            error: null,
          },
          {
            branch_id: "b_default",
            branch_index: 1,
            criteria_type: null,
            original_expression: null,
            rendered_expression: null,
            result: null,
            is_matched: false,
            is_default: true,
            next_block_label: "fallback",
            error: null,
          },
        ],
      },
    });

    render(<BlockDetailConditional block={block} />);
    // The keywords carry the order, so there is no hint sentence.
    expect(screen.getByText("if")).toBeDefined();
    expect(screen.getByText("else")).toBeDefined();
    expect(screen.queryByText(/first match/i)).toBeNull();
    // One emphasized outcome: the destination on the branch that ran.
    expect(screen.getAllByRole("img", { name: "taken" })).toHaveLength(1);
    expect(screen.getByText("notify_team")).toBeDefined();
    expect(screen.queryByText("fallback")).toBeNull();
    // A condition that held shows no bare result; no Result/Matched prose.
    expect(screen.queryByText(/Result/)).toBeNull();
    expect(screen.queryByText(/Matched/)).toBeNull();
    // The rendered value leads the line; the template follows it.
    expect(screen.getByText("5 > 3")).toBeDefined();
    expect(screen.getByText("{{ count > 3 }}")).toBeDefined();
  });

  it("falls back to the legacy executed_branch_expression rendering when no evaluations array", () => {
    // The shape a conditional evaluated from cached code writes.
    const block = buildConditional({
      executed_branch_id: "b_match",
      executed_branch_expression: "{{ x == 1 }}",
      executed_branch_result: true,
      output: {
        branch_taken: "next",
        branch_index: 0,
        next_block_label: "next",
      },
    });

    render(<BlockDetailConditional block={block} />);
    expect(screen.getByText(/^evaluation$/i)).toBeDefined();
    expect(screen.getByText("{{ x == 1 }}")).toBeDefined();
    expect(screen.queryByText(/could not be evaluated/i)).toBeNull();
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
    expect(screen.getByRole("button", { name: "Search JSON" })).toBeDefined();
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

  it("says a condition could not be evaluated and the default branch was taken, though the block completed", () => {
    const error =
      "Failed to evaluate natural language branches: Branch evaluation failed: LLM exploded";
    const block = buildConditional({
      status: Status.Completed,
      executed_branch_id: "b_default",
      executed_branch_next_block: "fallback_block",
      output: {
        branch_taken: "fallback_block",
        evaluations: [
          evaluation({
            branch_id: "b_prompt",
            criteria_type: "prompt",
            original_expression: "user selected premium plan",
            next_block_label: "premium",
            error,
          }),
          evaluation({
            branch_id: "b_default",
            is_default: true,
            is_matched: true,
            criteria_type: null,
            next_block_label: "fallback_block",
          }),
        ],
        evaluation_error: error,
      },
    });

    render(<BlockDetailConditional block={block} />);

    const notice = screen.getByText(/could not be evaluated/i);
    expect(notice.textContent).toMatch(/default branch/i);
    // The reason is stated once: the branch does not repeat what the notice says.
    expect(screen.getAllByText(error)).toHaveLength(1);
    const erroredRow = screen
      .getByText("user selected premium plan")
      .closest("li")!;
    expect(within(erroredRow).getByText("error")).toBeDefined();
    expect(screen.getByText("fallback_block")).toBeDefined();
  });

  it("marks an errored branch differently from one that evaluated false, and shows an error the notice does not state", () => {
    const promptError =
      "Failed to evaluate natural language branches: Branch evaluation failed: LLM exploded";
    const jinjaError =
      "Failed to format Jinja style parameter '{{ total > }}'. Reason: unexpected 'end of print statement'. <img src=x onerror=alert(1)>";
    const block = buildConditional({
      executed_branch_id: "b_default",
      executed_branch_next_block: "fallback_block",
      output: {
        evaluations: [
          evaluation({
            branch_id: "b_false",
            original_expression: "{{ total > 100 }}",
            result: false,
            next_block_label: "big_order",
          }),
          evaluation({
            branch_id: "b_prompt",
            criteria_type: "prompt",
            original_expression: "the page shows an invoice",
            next_block_label: "invoice",
            error: promptError,
          }),
          evaluation({
            branch_id: "b_jinja_error",
            original_expression: "{{ total > }}",
            next_block_label: "other",
            error: jinjaError,
          }),
          evaluation({
            branch_id: "b_default",
            is_default: true,
            is_matched: true,
            criteria_type: null,
            next_block_label: "fallback_block",
          }),
        ],
        evaluation_error: promptError,
      },
    });

    const { container } = render(<BlockDetailConditional block={block} />);

    const falseRow = screen.getByText("{{ total > 100 }}").closest("li")!;
    expect(within(falseRow).getByText("false")).toBeDefined();
    expect(within(falseRow).queryByText("error")).toBeNull();

    const jinjaRow = screen.getByText("{{ total > }}").closest("li")!;
    expect(within(jinjaRow).getByText("error")).toBeDefined();
    expect(within(jinjaRow).queryByText("false")).toBeNull();
    // Rendered as text: the markup in the message stays a string.
    expect(within(jinjaRow).getByText(jinjaError)).toBeDefined();
    expect(container.querySelector("img")).toBeNull();

    const promptRow = screen
      .getByText("the page shows an invoice")
      .closest("li")!;
    expect(within(promptRow).getByText("error")).toBeDefined();
    expect(screen.getAllByText(promptError)).toHaveLength(1);
  });

  it("does not claim the default branch was taken when a later branch matched after the error", () => {
    const branchError =
      "Failed to format Jinja style parameter '{{ total > }}'. Reason: unexpected 'end of print statement'.";
    const block = buildConditional({
      executed_branch_id: "b_true",
      executed_branch_expression: "{{ total > 10 }}",
      executed_branch_result: true,
      executed_branch_next_block: "big_order",
      output: {
        evaluations: [
          evaluation({
            branch_id: "b_error",
            original_expression: "{{ total > }}",
            next_block_label: "invoice",
            error: branchError,
          }),
          evaluation({
            branch_id: "b_true",
            original_expression: "{{ total > 10 }}",
            rendered_expression: "12 > 10",
            result: true,
            is_matched: true,
            next_block_label: "big_order",
          }),
        ],
        evaluation_error: `Failed to evaluate branch 0 for route_document: ${branchError}`,
      },
    });

    render(<BlockDetailConditional block={block} />);

    const notice = screen.getByText(/could not be evaluated/i);
    expect(notice.textContent).not.toMatch(/default branch/i);
    expect(
      screen.getByText(
        `Failed to evaluate branch 0 for route_document: ${branchError}`,
      ),
    ).toBeDefined();
  });

  it("renders no evaluation-error notice or error marker when the output carries no error", () => {
    const block = buildConditional({
      executed_branch_id: "b_true",
      output: {
        evaluations: [
          evaluation({
            branch_id: "b_true",
            original_expression: "{{ count > 3 }}",
            rendered_expression: "5 > 3",
            result: true,
            is_matched: true,
            next_block_label: "notify_team",
          }),
          evaluation({
            branch_id: "b_default",
            is_default: true,
            criteria_type: null,
            next_block_label: "fallback",
          }),
        ],
      },
    });

    render(<BlockDetailConditional block={block} />);

    expect(screen.getByText("notify_team")).toBeDefined();
    expect(screen.queryByText(/could not be evaluated/i)).toBeNull();
    expect(screen.queryByText("error")).toBeNull();
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
