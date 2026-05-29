// @vitest-environment jsdom

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Status } from "@/api/types";
import type { WorkflowRunBlock } from "../../types/workflowRunTypes";
import { BlockDetailLoop } from "./BlockDetailLoop";

function buildLoopBlock(
  overrides: Partial<WorkflowRunBlock> = {},
): WorkflowRunBlock {
  return {
    workflow_run_block_id: "wrb_loop",
    workflow_run_id: "wr_default",
    parent_workflow_run_block_id: null,
    block_type: "for_loop",
    label: "iterate_items",
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
    loop_values: [
      { name: "alpha", value: 1 },
      { name: "beta", value: 2 },
      { name: "gamma", value: 3 },
    ],
    current_value: null,
    current_index: null,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
});

describe("BlockDetailLoop", () => {
  it("shows the full iterable list when no iteration is selected", () => {
    render(<BlockDetailLoop block={buildLoopBlock()} iterationIndex={null} />);
    // The Iterable values section header includes the count
    expect(screen.getByText(/iterable values \(3\)/i)).toBeDefined();
    // All three values render as code blocks
    expect(screen.getByText(/"name": "alpha"/)).toBeDefined();
    expect(screen.getByText(/"name": "beta"/)).toBeDefined();
    expect(screen.getByText(/"name": "gamma"/)).toBeDefined();
  });

  it("hides the full iterable list when a specific iteration is selected", () => {
    render(<BlockDetailLoop block={buildLoopBlock()} iterationIndex={1} />);
    // The iterable list header should not appear
    expect(screen.queryByText(/iterable values \(3\)/i)).toBeNull();
  });

  it("shows the selected iteration's value section when iterationIndex is set", () => {
    render(<BlockDetailLoop block={buildLoopBlock()} iterationIndex={2} />);
    expect(screen.getByText(/iteration 3 value/i)).toBeDefined();
    expect(screen.getByText(/"name": "gamma"/)).toBeDefined();
    // alpha and beta should not render in this view
    expect(screen.queryByText(/"name": "alpha"/)).toBeNull();
    expect(screen.queryByText(/"name": "beta"/)).toBeNull();
  });

  it("falls back to the loop block's own current_index when no iteration override", () => {
    render(
      <BlockDetailLoop
        block={buildLoopBlock({ current_index: 1 })}
        iterationIndex={null}
      />,
    );
    // current iteration section: 2 of 3
    expect(screen.getByText(/current iteration/i)).toBeDefined();
    expect(screen.getByText(/2 of 3/)).toBeDefined();
  });

  it("falls back to the default loop view when iterationIndex is out of range", () => {
    // Loop has 3 values; URL says iteration=99. The iteration-only section
    // can't render (no value at index 99), and the default view must take
    // over so the body isn't empty.
    render(
      <BlockDetailLoop
        block={buildLoopBlock({ current_index: 2 })}
        iterationIndex={99}
      />,
    );
    // Iteration-specific section should NOT render
    expect(screen.queryByText(/iteration 100 value/i)).toBeNull();
    // Default view renders instead
    expect(screen.getByText(/iterable values \(3\)/i)).toBeDefined();
    expect(screen.getByText(/current iteration/i)).toBeDefined();
  });

  it("shows the selected while_loop iteration even without loop_values", () => {
    const whileBlock = buildLoopBlock({
      block_type: "while_loop",
      loop_values: null,
      current_index: 3,
    });
    render(<BlockDetailLoop block={whileBlock} iterationIndex={2} />);
    expect(screen.queryByText(/iteration 3 value/i)).toBeNull();
    expect(screen.getByText(/selected iteration/i)).toBeDefined();
    expect(screen.getByText("3")).toBeDefined();
  });
});
