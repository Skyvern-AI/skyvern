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

  it("renders action rows under a leaf block and lets the user select an action", () => {
    const onActionClick = vi.fn();
    const block = buildBlock({
      workflow_run_block_id: "wrb_action_block",
      block_type: "task_v2",
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

  it("renders action rows under a container block without requiring expansion", () => {
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

    expect(
      screen.getByText(/Extract the condition result from the page/),
    ).toBeDefined();
  });

  it("renders extract action rows for conditional blocks", () => {
    const onActionClick = vi.fn();
    const block = buildBlock({
      workflow_run_block_id: "wrb_condition_action",
      block_type: "conditional",
      label: "check_signin_ok",
      actions: [
        {
          action_id: "act_condition_extract",
          action_type: ActionTypes.extract,
          status: Status.Completed,
          reasoning: "Extract whether sign-in succeeded from the page",
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
    expect(screen.getByText("Extract Data")).toBeDefined();
    expect(
      screen.getByText(/Extract whether sign-in succeeded from the page/),
    ).toBeDefined();

    fireEvent.click(screen.getByRole("button", { name: /#1/i }));

    expect(onActionClick).toHaveBeenCalledWith({
      block,
      action: expect.objectContaining({ action_id: "act_condition_extract" }),
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
});
