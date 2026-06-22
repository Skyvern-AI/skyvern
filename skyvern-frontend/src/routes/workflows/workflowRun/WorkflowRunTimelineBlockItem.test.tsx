// @vitest-environment jsdom

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ActionTypes, Status } from "@/api/types";
import type {
  WorkflowRunBlock,
  WorkflowRunTimelineBlockItem as TimelineBlockItem,
  WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";
import type { CodeBlockStep } from "../types/workflowTypes";
import { WorkflowRunTimelineBlockItem } from "./WorkflowRunTimelineBlockItem";

function buildBlock(
  overrides: Partial<WorkflowRunBlock> = {},
): WorkflowRunBlock {
  return {
    workflow_run_block_id: "wrb_default",
    workflow_run_id: "wr_default",
    parent_workflow_run_block_id: null,
    block_type: "http_request",
    label: null,
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

function buildBlockItem(
  block: WorkflowRunBlock,
  children: Array<WorkflowRunTimelineItem> = [],
): TimelineBlockItem {
  return {
    type: "block",
    block,
    children,
    thought: null,
    created_at: block.created_at,
    modified_at: block.modified_at,
  };
}

const noop = () => {};

afterEach(() => {
  cleanup();
});

describe("WorkflowRunTimelineBlockItem", () => {
  it("highlights the block row when activeItem matches the block id", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_active",
      block_type: "http_request",
      label: "fetch_token",
    });
    render(
      <WorkflowRunTimelineBlockItem
        activeItem={block}
        block={block}
        subItems={[]}
        onActionClick={noop}
        onBlockItemClick={noop}
      />,
    );
    // The row's body button reports the active state via aria-pressed
    const rowButton = screen.getByRole("button", { name: /fetch_token/i });
    expect(rowButton.getAttribute("aria-pressed")).toBe("true");
  });

  it("renders a fixed row anatomy with order, type, descriptor, and action count", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_active",
      block_type: "conditional",
      label: "block_5",
      description: "Planning to branch based on {{bulk_download}} condition.",
      actions: [
        { action_id: "act_1" },
        { action_id: "act_2" },
      ] as unknown as WorkflowRunBlock["actions"],
    });

    render(
      <WorkflowRunTimelineBlockItem
        activeItem={block}
        block={block}
        blockOrder={new Map([[block.workflow_run_block_id, 5]])}
        subItems={[]}
        onActionClick={noop}
        onBlockItemClick={noop}
      />,
    );

    expect(screen.getByText("#5")).toBeDefined();
    expect(screen.getByText("Condition")).toBeDefined();
    expect(screen.getByText("block_5")).toBeDefined();
    expect(
      screen.getByText(
        /Planning to branch based on {{bulk_download}} condition\./,
      ),
    ).toBeDefined();
    expect(screen.getByText("2 actions")).toBeDefined();
  });

  it("omits the action count badge when a block has no actions", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_no_actions",
      block_type: "http_request",
      label: "fetch_token",
      actions: [],
    });

    render(
      <WorkflowRunTimelineBlockItem
        activeItem={block}
        block={block}
        subItems={[]}
        onActionClick={noop}
        onBlockItemClick={noop}
      />,
    );

    expect(screen.queryByText("0 actions")).toBeNull();
  });

  it("renders action rows under a code block and lets the user select an action", () => {
    const onActionClick = vi.fn();
    const block = buildBlock({
      workflow_run_block_id: "wrb_action_block",
      block_type: "code",
      label: "Open account page",
      actions: [
        {
          action_id: "act_second",
          action_type: ActionTypes.Click,
          status: Status.Completed,
          reasoning: "Click the account menu",
          created_by: null,
          confidence_float: null,
        },
        {
          action_id: "act_first",
          action_type: ActionTypes.extract,
          status: Status.Completed,
          reasoning: "Extract the calendar event date from the page",
          created_by: null,
          confidence_float: 1,
        },
      ] as unknown as WorkflowRunBlock["actions"],
    });

    render(
      <WorkflowRunTimelineBlockItem
        activeItem={block}
        block={block}
        subItems={[]}
        onActionClick={onActionClick}
        onBlockItemClick={noop}
      />,
    );

    const firstAction = screen.getByText(
      /Extract the calendar event date from the page/,
    );
    const secondAction = screen.getByText(/Click the account menu/);
    expect(screen.getByText("Extract Data")).toBeDefined();
    expect(screen.queryByText("100%")).toBeNull();
    expect(
      firstAction.compareDocumentPosition(secondAction) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /#1/i }));

    expect(onActionClick).toHaveBeenCalledWith({
      block,
      action: expect.objectContaining({ action_id: "act_first" }),
    });
  });

  it("renders child blocks and action rows under a non-code container block", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_container_with_actions",
      block_type: "conditional",
      label: "Validate report",
      actions: [
        {
          action_id: "act_extract",
          action_type: ActionTypes.extract,
          status: Status.Completed,
          reasoning: "Extract the condition result from the page",
          created_by: null,
          confidence_float: 1,
        },
      ] as unknown as WorkflowRunBlock["actions"],
    });
    const child = buildBlock({
      workflow_run_block_id: "wrb_child",
      block_type: "text_prompt",
      label: "Next step",
    });

    render(
      <WorkflowRunTimelineBlockItem
        activeItem={block}
        block={block}
        subItems={[buildBlockItem(child)]}
        onActionClick={vi.fn()}
        onBlockItemClick={noop}
      />,
    );

    expect(screen.getByText("Next step")).toBeDefined();
    expect(
      screen.getByText(/Extract the condition result from the page/),
    ).toBeDefined();
  });

  it("renders action rows under a non-code leaf block and lets the user select an action", () => {
    const onActionClick = vi.fn();
    const block = buildBlock({
      workflow_run_block_id: "wrb_login_actions",
      block_type: "login",
      label: "block_1",
      actions: [
        {
          action_id: "act_login_click",
          action_type: ActionTypes.Click,
          status: Status.Completed,
          reasoning: "Click the login link in the top navigation",
          created_by: null,
          confidence_float: 1,
        },
      ] as unknown as WorkflowRunBlock["actions"],
    });

    render(
      <WorkflowRunTimelineBlockItem
        activeItem={block}
        block={block}
        subItems={[]}
        onActionClick={onActionClick}
        onBlockItemClick={noop}
      />,
    );

    expect(screen.getByText("1 action")).toBeDefined();
    expect(
      screen.getByText(/Click the login link in the top navigation/),
    ).toBeDefined();

    fireEvent.click(screen.getByRole("button", { name: /#1/i }));

    expect(onActionClick).toHaveBeenCalledWith({
      block,
      action: expect.objectContaining({ action_id: "act_login_click" }),
    });
  });

  it("renders loop iterations from first to last", () => {
    const iterChildA = buildBlock({
      workflow_run_block_id: "wrb_iter1_leaf",
      block_type: "http_request",
      current_index: 1,
    });
    const iterChildB = buildBlock({
      workflow_run_block_id: "wrb_iter0_leaf",
      block_type: "http_request",
      current_index: 0,
    });
    const loop = buildBlock({
      workflow_run_block_id: "wrb_loop",
      block_type: "for_loop",
      label: "iterate_items",
      loop_values: ["alpha", "beta"],
    });

    render(
      <WorkflowRunTimelineBlockItem
        activeItem={loop}
        activeIteration={0}
        block={loop}
        subItems={[buildBlockItem(iterChildA), buildBlockItem(iterChildB)]}
        onActionClick={noop}
        onBlockItemClick={noop}
        onIterationClick={noop}
      />,
    );

    const iterations = screen.getAllByText(/iteration \d/i);
    expect(iterations.map((node) => node.textContent)).toEqual([
      "Iteration 1",
      "Iteration 2",
    ]);
  });

  it("expands a deep-linked loop with a selected iteration on initial mount", () => {
    // Two iteration's worth of children — both should be revealed.
    const iterChildA = buildBlock({
      workflow_run_block_id: "wrb_iter1_leaf",
      block_type: "http_request",
      current_index: 1,
    });
    const iterChildB = buildBlock({
      workflow_run_block_id: "wrb_iter0_leaf",
      block_type: "http_request",
      current_index: 0,
    });
    const loop = buildBlock({
      workflow_run_block_id: "wrb_loop",
      block_type: "for_loop",
      label: "iterate_items",
      loop_values: ["alpha", "beta"],
    });
    render(
      <WorkflowRunTimelineBlockItem
        activeItem={loop}
        activeIteration={0}
        block={loop}
        subItems={[buildBlockItem(iterChildA), buildBlockItem(iterChildB)]}
        onActionClick={noop}
        onBlockItemClick={noop}
        onIterationClick={noop}
      />,
    );
    // Two iteration rows visible → loop expanded → R10 fix verified
    expect(screen.getAllByText(/iteration \d/i).length).toBeGreaterThanOrEqual(
      2,
    );
  });

  it("appends code line and duration to recorded action summaries in code blocks", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code",
      block_type: "code",
      label: "run_script",
      actions: [
        // Newest-first payload, matching the task-action DESC convention.
        {
          action_id: "wrb_code_action_1",
          action_type: ActionTypes.Click,
          status: Status.Completed,
          reasoning: null,
          description: "locator.click #submit",
          output: { code_line: 3, duration_ms: 1500 },
          created_by: null,
          confidence_float: null,
        },
        {
          action_id: "wrb_code_action_0",
          action_type: ActionTypes.GotoUrl,
          status: Status.Completed,
          reasoning: null,
          description: "page.goto https://example.com",
          output: { code_line: 1, duration_ms: 65000 },
          created_by: null,
          confidence_float: null,
        },
      ] as unknown as WorkflowRunBlock["actions"],
    });

    render(
      <WorkflowRunTimelineBlockItem
        activeItem={block}
        block={block}
        subItems={[]}
        onActionClick={noop}
        onBlockItemClick={noop}
      />,
    );

    const gotoRow = screen.getByText(
      /page\.goto https:\/\/example\.com · line 1 · 1m 5s/,
    );
    const clickRow = screen.getByText(
      /locator\.click #submit · line 3 · 1\.5s/,
    );
    expect(
      gotoRow.compareDocumentPosition(clickRow) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("labels a recorded page.evaluate action as Execute JS instead of a blank badge", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_eval",
      block_type: "code",
      label: "run_script",
      actions: [
        {
          action_id: "wrb_code_eval_action_0",
          action_type: "execute_js",
          status: Status.Completed,
          reasoning: null,
          description: "page.evaluate () => document.title",
          output: { code_line: 3, duration_ms: 800 },
          created_by: null,
          confidence_float: null,
        },
      ] as unknown as WorkflowRunBlock["actions"],
    });

    render(
      <WorkflowRunTimelineBlockItem
        activeItem={block}
        block={block}
        subItems={[]}
        onActionClick={noop}
        onBlockItemClick={noop}
      />,
    );

    expect(screen.getByText("Execute JS")).toBeDefined();
  });

  it("humanizes an unmapped recorded action type rather than rendering a blank badge", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_unmapped",
      block_type: "code",
      label: "run_script",
      actions: [
        {
          action_id: "wrb_code_unmapped_action_0",
          action_type: "go_forward",
          status: Status.Completed,
          reasoning: null,
          description: "page.go_forward",
          output: { code_line: 2, duration_ms: 100 },
          created_by: null,
          confidence_float: null,
        },
      ] as unknown as WorkflowRunBlock["actions"],
    });

    render(
      <WorkflowRunTimelineBlockItem
        activeItem={block}
        block={block}
        subItems={[]}
        onActionClick={noop}
        onBlockItemClick={noop}
      />,
    );

    expect(screen.getByText("Go Forward")).toBeDefined();
  });

  it("labels the synthetic code error row as Error instead of Screenshot", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_failed",
      block_type: "code",
      label: "run_script",
      status: Status.Failed,
      actions: [
        {
          action_id: "wrb_code_failed_action_0",
          action_type: ActionTypes.NullAction,
          status: Status.Failed,
          reasoning: null,
          description: "code error at line 7",
          response: "ValueError: boom",
          output: { code_line: 7 },
          created_by: null,
          confidence_float: null,
        },
      ] as unknown as WorkflowRunBlock["actions"],
    });

    render(
      <WorkflowRunTimelineBlockItem
        activeItem={block}
        block={block}
        subItems={[]}
        onActionClick={noop}
        onBlockItemClick={noop}
      />,
    );

    expect(screen.getByText("Error")).toBeDefined();
    expect(screen.queryByText("Screenshot")).toBeNull();
    expect(screen.getByText(/ValueError: boom · line 7/)).toBeDefined();
  });

  it("renders non-code action rows without code line metadata", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_task_null_action",
      block_type: "task_v2",
      label: "Capture page",
      actions: [
        {
          action_id: "act_screenshot",
          action_type: ActionTypes.NullAction,
          status: Status.Failed,
          reasoning: "Capture failed",
          output: { code_line: 5, duration_ms: 2000 },
          created_by: null,
          confidence_float: null,
        },
      ] as unknown as WorkflowRunBlock["actions"],
    });

    render(
      <WorkflowRunTimelineBlockItem
        activeItem={block}
        block={block}
        subItems={[]}
        onActionClick={noop}
        onBlockItemClick={noop}
      />,
    );

    expect(screen.getByText("Screenshot")).toBeDefined();
    expect(screen.queryByText("Error")).toBeNull();
    expect(screen.getByText(/Capture failed/)).toBeDefined();
    expect(screen.queryByText(/line 5/)).toBeNull();
  });

  it("keeps an iteration collapsed after the user clicks the chevron, even when an active descendant appears", () => {
    // Render an expanded iteration (groupIndex 0 default-opens), then user
    // collapses via chevron. Re-render the same component with an active
    // child — the userToggledRef guard should keep it collapsed.
    const iterChild = buildBlock({
      workflow_run_block_id: "wrb_iter0_child",
      block_type: "http_request",
      current_index: 0,
    });
    const loop = buildBlock({
      workflow_run_block_id: "wrb_loop",
      block_type: "for_loop",
      label: "iterate_items",
      loop_values: ["alpha"],
    });
    const subItems = [buildBlockItem(iterChild)];
    const { rerender } = render(
      <WorkflowRunTimelineBlockItem
        activeItem={loop}
        activeIteration={0}
        block={loop}
        subItems={subItems}
        onActionClick={noop}
        onBlockItemClick={noop}
        onIterationClick={noop}
      />,
    );
    // Iteration row's chevron — collapse it
    const collapseButton = screen.getByRole("button", {
      name: /collapse iteration/i,
    });
    fireEvent.click(collapseButton);

    // Re-render with the active item flipped to the child block, which
    // would otherwise trigger the auto-expand effect (hasActiveDescendant).
    rerender(
      <WorkflowRunTimelineBlockItem
        activeItem={iterChild}
        activeIteration={0}
        block={loop}
        subItems={subItems}
        onActionClick={noop}
        onBlockItemClick={noop}
        onIterationClick={noop}
      />,
    );
    // Chevron stays in "Expand" state (collapsed) thanks to userToggledRef
    expect(
      screen.queryByRole("button", { name: /collapse iteration/i }),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: /expand iteration/i }),
    ).toBeDefined();
  });

  it("renders the code block step outline when the block has no recorded actions", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_outline",
      block_type: "code",
      label: "run_script",
      actions: [],
    });
    const steps: Array<CodeBlockStep> = [
      {
        action_type: "goto",
        title: "Open the homepage",
        line_start: 1,
        line_end: 1,
      },
      {
        action_type: "click",
        description: "Click the top post",
        line_start: 3,
        line_end: 5,
      },
    ];

    render(
      <WorkflowRunTimelineBlockItem
        activeItem={block}
        block={block}
        subItems={[]}
        codeStepsByLabel={new Map([["run_script", steps]])}
        onActionClick={noop}
        onBlockItemClick={noop}
      />,
    );

    expect(screen.getByText(/Open the homepage/)).toBeDefined();
    expect(screen.getByText(/Click the top post/)).toBeDefined();
    expect(screen.getByText("goto")).toBeDefined();
    expect(screen.getByText("click")).toBeDefined();
    expect(screen.getByText("L1")).toBeDefined();
    expect(screen.getByText("L3-5")).toBeDefined();
  });

  it("selects the block when a code step row is clicked", () => {
    const onBlockItemClick = vi.fn();
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_outline_click",
      block_type: "code",
      label: "run_script",
      actions: [],
    });
    const steps: Array<CodeBlockStep> = [
      { action_type: "goto", title: "Open the homepage", line_start: 1 },
    ];

    render(
      <WorkflowRunTimelineBlockItem
        activeItem={block}
        block={block}
        subItems={[]}
        codeStepsByLabel={new Map([["run_script", steps]])}
        onActionClick={noop}
        onBlockItemClick={onBlockItemClick}
      />,
    );

    fireEvent.click(screen.getByText(/Open the homepage/));
    expect(onBlockItemClick).toHaveBeenCalledWith(block);
  });

  it("prefers recorded actions over the step outline for code blocks", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_actions_win",
      block_type: "code",
      label: "run_script",
      actions: [
        {
          action_id: "wrb_code_action_0",
          action_type: ActionTypes.GotoUrl,
          status: Status.Completed,
          reasoning: null,
          description: "page.goto https://example.com",
          output: { code_line: 1, duration_ms: 500 },
          created_by: null,
          confidence_float: null,
        },
      ] as unknown as WorkflowRunBlock["actions"],
    });
    const steps: Array<CodeBlockStep> = [
      { action_type: "goto", title: "Outline step that should be hidden" },
    ];

    render(
      <WorkflowRunTimelineBlockItem
        activeItem={block}
        block={block}
        subItems={[]}
        codeStepsByLabel={new Map([["run_script", steps]])}
        onActionClick={noop}
        onBlockItemClick={noop}
      />,
    );

    expect(
      screen.getByText(/page\.goto https:\/\/example\.com · line 1/),
    ).toBeDefined();
    expect(screen.queryByText("Outline step that should be hidden")).toBeNull();
  });
});
