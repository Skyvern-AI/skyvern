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

  it("labels non-failed synthetic code rows as steps instead of screenshots", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_synthetic",
      block_type: "code",
      label: "run_script",
      actions: [
        {
          action_id: "wrb_code_synthetic_action_0",
          action_type: ActionTypes.NullAction,
          status: Status.Completed,
          reasoning: null,
          description: "recorded code step",
          output: { code_line: 4, duration_ms: 250 },
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

    expect(screen.getByText("Step")).toBeDefined();
    expect(screen.queryByText("Screenshot")).toBeNull();
    expect(screen.queryByText("Error")).toBeNull();
    expect(
      screen.getByText(/recorded code step · line 4 · 0.3s/),
    ).toBeDefined();
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
      prompt: "Run homepage flow",
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
    expect(screen.getByText("Goto")).toBeDefined();
    expect(screen.getByText("Click")).toBeDefined();
    expect(screen.getByText("L1")).toBeDefined();
    expect(screen.getByText("L3-5")).toBeDefined();
  });

  it("uses the code block prompt as the row name", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_prompt_title",
      block_type: "code",
      label: "run_script",
      prompt: "Collect invoice details",
      actions: [],
    });
    const steps: Array<CodeBlockStep> = [
      { action_type: "execute_js", title: "Run a script" },
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

    expect(screen.getByText("Collect invoice details")).toBeDefined();
  });

  it("falls back to the first code step title for the row name", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_step_title",
      block_type: "code",
      label: "run_script",
      prompt: null,
      actions: [],
    });
    const steps: Array<CodeBlockStep> = [
      { action_type: "execute_js", title: "Summarize the page" },
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

    expect(screen.getByText("Summarize the page")).toBeDefined();
  });

  it("falls back to the block reasoning before bare 'Code' for prompt-less code blocks", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_reasoning",
      block_type: "code",
      label: "block_1",
      prompt: null,
      description: "Planning to extract current top post details.",
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

    // The reasoning is surfaced as the row name (it would otherwise be lost to
    // the bare "Code" fallback); the label remains the descriptor subtitle.
    expect(
      screen.getByText("Planning to extract current top post details."),
    ).toBeDefined();
  });

  it("prefers the code block prompt over the reasoning for the row name", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_prompt_over_reasoning",
      block_type: "code",
      label: "block_1",
      prompt: "Run the homepage flow",
      description: "Planning to extract current top post details.",
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

    expect(screen.getByText("Run the homepage flow")).toBeDefined();
    expect(
      screen.queryByText("Planning to extract current top post details."),
    ).toBeNull();
  });

  it("shows the bare 'Code' name for a prompt-less code block with no reasoning", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_bare",
      block_type: "code",
      label: "block_2",
      prompt: null,
      description: null,
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

    // Both the type badge and the row name read "Code" when nothing else exists.
    expect(screen.getAllByText("Code").length).toBe(2);
  });

  it("selects the block when a code step row is clicked", () => {
    const onBlockItemClick = vi.fn();
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_outline_click",
      block_type: "code",
      label: "run_script",
      prompt: "Run homepage flow",
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

  it("marks definition steps after the failure line as 'didn't run' in a failed code block", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_partial_fail",
      block_type: "code",
      label: "run_script",
      prompt: "Run the saved script",
      status: Status.Failed,
      actions: [
        // DESC payload: the synthetic error row is newest.
        {
          action_id: "wrb_code_err",
          action_type: ActionTypes.NullAction,
          status: Status.Failed,
          reasoning: null,
          description: "code error at line 3",
          response: "ValueError: boom",
          output: { code_line: 3 },
          created_by: null,
          confidence_float: null,
        },
        {
          action_id: "wrb_code_goto",
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
      { action_type: "goto", title: "Open the homepage", line_start: 1 },
      { action_type: "click", title: "Submit the form", line_start: 3 },
      { action_type: "extract", title: "Read the result", line_start: 5 },
      {
        action_type: "execute_js",
        title: "Summarize the page",
        line_start: 7,
        line_end: 8,
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

    // Only the steps strictly after the failure line (3) are "didn't run".
    const didntRun = screen.getAllByText(/didn't run/i);
    expect(didntRun.length).toBe(2);
    expect(screen.getByText(/Read the result/)).toBeDefined();
    expect(screen.getByText(/Summarize the page/)).toBeDefined();
    // The executed goto step (line 1) reads its plain-English step copy in its
    // fired action row, not as a didn't-run row.
    expect(screen.getByText(/Open the homepage · line 1/)).toBeDefined();
    // The step at the failure line (3) executed no fired action and is not after
    // the failure, so it surfaces nowhere.
    expect(screen.queryByText(/Submit the form/)).toBeNull();
    // Neutral muted tone — never the rose error tone.
    expect(didntRun[0]!.className).toMatch(/text-muted-foreground/);
    expect(didntRun[0]!.className).not.toMatch(/rose/);
  });

  it("renders no 'didn't run' rows when the code block fails at or after its last step", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_fail_last",
      block_type: "code",
      label: "run_script",
      status: Status.Failed,
      actions: [
        {
          action_id: "wrb_code_err_last",
          action_type: ActionTypes.NullAction,
          status: Status.Failed,
          reasoning: null,
          description: "code error at line 9",
          response: "Boom",
          output: { code_line: 9 },
          created_by: null,
          confidence_float: null,
        },
      ] as unknown as WorkflowRunBlock["actions"],
    });
    const steps: Array<CodeBlockStep> = [
      { action_type: "goto", title: "Open the homepage", line_start: 1 },
      { action_type: "click", title: "Submit the form", line_start: 3 },
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

    expect(screen.queryByText(/didn't run/i)).toBeNull();
  });

  it("skips definition steps without a line position when marking 'didn't run'", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_null_line",
      block_type: "code",
      label: "run_script",
      prompt: "Run the saved script",
      status: Status.Failed,
      actions: [
        {
          action_id: "wrb_code_err_null",
          action_type: ActionTypes.NullAction,
          status: Status.Failed,
          reasoning: null,
          description: "code error at line 2",
          response: "Boom",
          output: { code_line: 2 },
          created_by: null,
          confidence_float: null,
        },
      ] as unknown as WorkflowRunBlock["actions"],
    });
    const steps: Array<CodeBlockStep> = [
      { action_type: "extract", title: "Has a line position", line_start: 5 },
      { action_type: "execute_js", title: "No line position" },
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

    expect(screen.getAllByText(/didn't run/i).length).toBe(1);
    expect(screen.getByText(/Has a line position/)).toBeDefined();
    expect(screen.queryByText(/No line position/)).toBeNull();
  });

  it("does not mark steps as 'didn't run' for a successful code block", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_success",
      block_type: "code",
      label: "run_script",
      status: Status.Completed,
      actions: [
        {
          action_id: "wrb_code_ok",
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
      { action_type: "extract", title: "A later step", line_start: 5 },
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

    expect(screen.queryByText(/didn't run/i)).toBeNull();
  });

  it("does not infer skipped steps when a failed code block has no synthetic error row", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_failed_without_error_row",
      block_type: "code",
      label: "run_script",
      prompt: "Run cached code",
      status: Status.Failed,
      actions: [
        {
          action_id: "wrb_code_goto_only",
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
      { action_type: "goto", title: "Open the homepage", line_start: 1 },
      { action_type: "extract", title: "Read the result", line_start: 5 },
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

    expect(screen.queryByText(/didn't run/i)).toBeNull();
    expect(screen.queryByText(/Read the result/)).toBeNull();
  });

  it("prefers recorded actions over the step outline for code blocks", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_actions_win",
      block_type: "code",
      label: "run_script",
      prompt: "Run cached code",
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

  it("leads a fired code action row with the matched definition step's plain English", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_plain_english",
      block_type: "code",
      label: "run_script",
      prompt: "Run cached code",
      actions: [
        {
          action_id: "wrb_code_extract",
          action_type: ActionTypes.extract,
          status: Status.Completed,
          reasoning: null,
          description: "page.extract",
          output: { code_line: 12, duration_ms: 500 },
          created_by: null,
          confidence_float: null,
        },
      ] as unknown as WorkflowRunBlock["actions"],
    });
    const steps: Array<CodeBlockStep> = [
      {
        action_type: "extract",
        title: "Extract the product details",
        line_start: 12,
        line_end: 12,
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

    // The same plain-English copy as the editor, not the raw page.extract call.
    expect(
      screen.getByText(/Extract the product details · line 12/),
    ).toBeDefined();
    expect(screen.queryByText(/page\.extract/)).toBeNull();
    // The readable action type stays as the chip.
    expect(screen.getByText("Extract Data")).toBeDefined();
  });

  it("matches a fired code action to a multi-line step by range containment", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_range",
      block_type: "code",
      label: "run_script",
      prompt: "Run cached code",
      actions: [
        {
          action_id: "wrb_code_range_action",
          action_type: ActionTypes.Click,
          status: Status.Completed,
          reasoning: null,
          description: "page.click",
          output: { code_line: 4, duration_ms: 200 },
          created_by: null,
          confidence_float: null,
        },
      ] as unknown as WorkflowRunBlock["actions"],
    });
    const steps: Array<CodeBlockStep> = [
      {
        action_type: "click",
        title: "Submit the application",
        line_start: 3,
        line_end: 6,
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

    expect(screen.getByText(/Submit the application · line 4/)).toBeDefined();
  });

  it("falls back to the readable action type when no definition step matches a fired code action", () => {
    const block = buildBlock({
      workflow_run_block_id: "wrb_code_no_match",
      block_type: "code",
      label: "run_script",
      prompt: "Run cached code",
      actions: [
        {
          action_id: "wrb_code_no_match_action",
          action_type: ActionTypes.extract,
          status: Status.Completed,
          reasoning: null,
          description: null,
          output: { code_line: 99 },
          created_by: null,
          confidence_float: null,
        },
      ] as unknown as WorkflowRunBlock["actions"],
    });
    const steps: Array<CodeBlockStep> = [
      {
        action_type: "extract",
        title: "A step on another line",
        line_start: 1,
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

    // No matching step and no reasoning — the readable action type carries the row.
    expect(screen.getByText("Extract Data")).toBeDefined();
    expect(screen.queryByText(/A step on another line/)).toBeNull();
  });
});
