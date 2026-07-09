// @vitest-environment jsdom

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Status } from "@/api/types";
import type {
  ObserverThought,
  WorkflowRunBlock,
} from "../../types/workflowRunTypes";
import { BlockDetailTask } from "./BlockDetailTask";

function buildTaskBlock(
  overrides: Partial<WorkflowRunBlock> = {},
): WorkflowRunBlock {
  return {
    workflow_run_block_id: "wrb_task",
    workflow_run_id: "wr_default",
    parent_workflow_run_block_id: null,
    block_type: "task",
    label: "task_block",
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

function buildThought(
  overrides: Partial<ObserverThought> = {},
): ObserverThought {
  return {
    thought_id: "thg_1",
    observer_cruise_id: "oc_default",
    organization_id: "org_default",
    user_input: null,
    observation: null,
    thought: "Considering the next step",
    answer: null,
    workflow_run_id: "wr_default",
    workflow_id: "w_default",
    workflow_permanent_id: "wpid_default",
    workflow_run_block_id: "wrb_task",
    task_id: null,
    created_at: "2026-01-01T00:00:01Z",
    modified_at: "2026-01-01T00:00:01Z",
    ...(overrides as Partial<ObserverThought>),
  } as ObserverThought;
}

afterEach(() => {
  cleanup();
});

describe("BlockDetailTask thought selection", () => {
  it("fires onThoughtSelect when a thought card is clicked", () => {
    const onThoughtSelect = vi.fn();
    const thought = buildThought({
      thought_id: "thg_42",
      thought: "Picking the right button",
    });

    render(
      <BlockDetailTask
        block={buildTaskBlock()}
        activeItem={null}
        thoughts={[thought]}
        onThoughtSelect={onThoughtSelect}
      />,
    );

    fireEvent.click(screen.getByText("Picking the right button"));

    expect(onThoughtSelect).toHaveBeenCalledTimes(1);
    expect(onThoughtSelect).toHaveBeenCalledWith(thought);
  });

  it("renders thought cards without crashing when no onThoughtSelect is given", () => {
    render(
      <BlockDetailTask
        block={buildTaskBlock()}
        activeItem={null}
        thoughts={[buildThought()]}
      />,
    );
    expect(screen.getByText("Considering the next step")).toBeDefined();
  });
});
