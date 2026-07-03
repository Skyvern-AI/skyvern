// @vitest-environment jsdom

import { renderHook } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { type ReactNode } from "react";

import { Status } from "@/api/types";
import { useRunViewStore } from "@/store/RunViewStore";
import type {
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";
import { useRunVisuals } from "./useRunVisuals";

const mocks = vi.hoisted(() => ({
  workflowRun: undefined as unknown,
  timeline: undefined as unknown,
}));

vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => ({
    data: mocks.workflowRun,
    isLoading: false,
  }),
}));
vi.mock("../hooks/useWorkflowRunTimelineQuery", () => ({
  useWorkflowRunTimelineQuery: () => ({
    data: mocks.timeline,
    isLoading: false,
  }),
}));

function buildBlock(
  overrides: Partial<WorkflowRunBlock> = {},
): WorkflowRunBlock {
  return {
    workflow_run_block_id: "wrb_default",
    workflow_run_id: "wr_default",
    parent_workflow_run_block_id: null,
    block_type: "task",
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
): WorkflowRunTimelineItem {
  return {
    type: "block",
    block,
    children,
    thought: null,
    created_at: block.created_at,
    modified_at: block.modified_at,
  };
}

function seedLoopRun() {
  const loop = buildBlock({
    workflow_run_block_id: "wrb_loop",
    block_type: "for_loop",
    label: "checkout-loop",
    loop_values: ["alpha", "beta"],
    current_index: 0,
    created_at: "2026-06-10T00:00:00Z",
  });
  const iter0 = buildBlock({
    workflow_run_block_id: "wrb_iter0",
    parent_workflow_run_block_id: "wrb_loop",
    current_index: 0,
    created_at: "2026-06-10T00:00:10Z",
  });
  const iter1 = buildBlock({
    workflow_run_block_id: "wrb_iter1",
    parent_workflow_run_block_id: "wrb_loop",
    current_index: 1,
    created_at: "2026-06-10T00:00:20Z",
  });
  mocks.timeline = [
    buildBlockItem(loop, [buildBlockItem(iter0), buildBlockItem(iter1)]),
  ];
  mocks.workflowRun = {
    workflow_run_id: "wr_1",
    status: Status.Completed,
    workflow: {
      workflow_definition: { blocks: [], finally_block_label: null },
    },
  };
}

function wrapper({ children }: { children: ReactNode }) {
  return (
    <MemoryRouter initialEntries={["/?active=wrb_loop"]}>
      {children}
    </MemoryRouter>
  );
}

afterEach(() => {
  mocks.workflowRun = undefined;
  mocks.timeline = undefined;
});
beforeEach(() => useRunViewStore.getState().reset());

describe("useRunVisuals loop-iteration threading", () => {
  test("resolves a selected container without an iteration to its first leaf", () => {
    seedLoopRun();
    const { result } = renderHook(() => useRunVisuals("wr_1"), { wrapper });

    expect(result.current.heroSelection).toMatchObject({
      kind: "block",
      workflowRunBlockId: "wrb_iter0",
    });
  });

  test("resolves the Run pane's selected iteration from the shared store", () => {
    seedLoopRun();
    useRunViewStore.getState().pinFrame("wrb_loop", 1);
    const { result } = renderHook(() => useRunVisuals("wr_1"), { wrapper });

    expect(result.current.heroSelection).toMatchObject({
      kind: "block",
      workflowRunBlockId: "wrb_iter1",
    });
  });
});
