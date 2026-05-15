// @vitest-environment jsdom

// AxiosClient reads env vars at module load time — stub it before the
// component tree (which transitively imports it) is resolved.
vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("@/hooks/useWorkflowRunViewingV2", () => ({
  useWorkflowRunViewingV2: vi.fn(),
}));

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { type ActionsApiResponse, Status } from "@/api/types";
import { useWorkflowRunViewingV2 } from "@/hooks/useWorkflowRunViewingV2";
import type { WorkflowRunBlock } from "../types/workflowRunTypes";
import { WorkflowRunTimelineBlockItem } from "./WorkflowRunTimelineBlockItem";

function buildAction(
  overrides: Partial<ActionsApiResponse> = {},
): ActionsApiResponse {
  return {
    action_id: "act_1",
    action_type: "click",
    status: Status.Completed,
    task_id: "task_1",
    step_id: "step_1",
    step_order: 0,
    action_order: 0,
    confidence_float: 0.9,
    description: null,
    reasoning: "Click the submit button",
    intention: null,
    response: null,
    created_by: null,
    text: null,
    ...overrides,
  };
}

function buildBlock(
  overrides: Partial<WorkflowRunBlock> = {},
): WorkflowRunBlock {
  return {
    workflow_run_block_id: "wrb_1",
    workflow_run_id: "wr_1",
    parent_workflow_run_block_id: null,
    block_type: "task",
    label: "Sample block",
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
    actions: [buildAction()],
    created_at: "2026-05-14T00:00:00Z",
    modified_at: "2026-05-14T00:00:00Z",
    duration: null,
    loop_values: null,
    current_value: null,
    current_index: null,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("WorkflowRunTimelineBlockItem action renderer", () => {
  it("renders the legacy ActionCard when the v2 flag is off", () => {
    vi.mocked(useWorkflowRunViewingV2).mockReturnValue(false);

    render(
      <WorkflowRunTimelineBlockItem
        activeItem={null}
        block={buildBlock()}
        subItems={[]}
        onBlockItemClick={() => {}}
        onActionClick={() => {}}
        onThoughtCardClick={() => {}}
      />,
    );

    expect(document.querySelector('[data-slot="runcard"]')).not.toBeNull();
    expect(
      document.querySelector('[data-slot="action-card-compact"]'),
    ).toBeNull();
  });

  it("renders ActionCardCompact when the v2 flag is on", () => {
    vi.mocked(useWorkflowRunViewingV2).mockReturnValue(true);

    render(
      <WorkflowRunTimelineBlockItem
        activeItem={null}
        block={buildBlock()}
        subItems={[]}
        onBlockItemClick={() => {}}
        onActionClick={() => {}}
        onThoughtCardClick={() => {}}
      />,
    );

    expect(
      document.querySelector('[data-slot="action-card-compact"]'),
    ).not.toBeNull();
    expect(document.querySelector('[data-slot="runcard"]')).toBeNull();
    expect(screen.getByText("Click the submit button")).toBeDefined();
  });

  it("clicking a compact action selects the action without selecting the block", () => {
    vi.mocked(useWorkflowRunViewingV2).mockReturnValue(true);
    const onActionClick = vi.fn();
    const onBlockItemClick = vi.fn();

    render(
      <WorkflowRunTimelineBlockItem
        activeItem={null}
        block={buildBlock()}
        subItems={[]}
        onBlockItemClick={onBlockItemClick}
        onActionClick={onActionClick}
        onThoughtCardClick={() => {}}
      />,
    );

    fireEvent.click(screen.getByText("Click the submit button"));

    expect(onActionClick).toHaveBeenCalledTimes(1);
    expect(onBlockItemClick).not.toHaveBeenCalled();
  });
});
