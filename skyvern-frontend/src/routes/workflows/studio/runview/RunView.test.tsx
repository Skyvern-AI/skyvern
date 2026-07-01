// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { type ReactNode } from "react";

import { ActionTypes, Status, type ActionsApiResponse } from "@/api/types";
import { useRunViewStore } from "@/store/RunViewStore";
import type {
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../../types/workflowRunTypes";
import { RunView } from "./RunView";

const mocks = vi.hoisted(() => ({
  workflowRun: undefined as unknown,
  timeline: undefined as unknown,
  runHeroProps: [] as Array<Record<string, unknown>>,
}));

vi.mock("../../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => ({
    data: mocks.workflowRun,
    isLoading: false,
  }),
}));
vi.mock("../../hooks/useWorkflowRunTimelineQuery", () => ({
  useWorkflowRunTimelineQuery: () => ({
    data: mocks.timeline,
    isLoading: false,
  }),
}));
vi.mock("../../hooks/useDebugSessionQuery", () => ({
  useDebugSessionQuery: () => ({ data: undefined }),
}));
vi.mock("../../editor/hooks/useIsGeneratingCode", () => ({
  useIsGeneratingCode: () => false,
}));
vi.mock("./RunHero", () => ({
  RunHero: (props: Record<string, unknown>) => {
    mocks.runHeroProps.push(props);
    return <div data-testid="run-hero" />;
  },
}));
vi.mock("../../workflowRun/WorkflowRunVerificationCodeForm", () => ({
  WorkflowRunVerificationCodeForm: () => null,
}));
// Radix ScrollArea needs ResizeObserver, which jsdom doesn't provide.
vi.mock("@/components/ui/scroll-area", () => ({
  ScrollArea: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  ScrollAreaViewport: ({ children }: { children?: ReactNode }) => (
    <div>{children}</div>
  ),
}));
vi.mock("posthog-js/react", () => ({
  usePostHog: () => ({ capture: vi.fn() }),
}));
vi.mock("@/hooks/useApiCredential", () => ({ useApiCredential: () => null }));

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

function buildAction(
  overrides: Partial<ActionsApiResponse> = {},
): ActionsApiResponse {
  return {
    action_id: "act_default",
    action_type: ActionTypes.Click,
    status: Status.Completed,
    intention: null,
    description: null,
    reasoning: null,
    step_id: "step_default",
    action_order: 0,
    screenshot_artifact_id: null,
    ...overrides,
  } as ActionsApiResponse;
}

function seedForLoopRun() {
  // current_index 0 keeps the header's fallback chip on "Iteration 1" so the
  // timeline's "Iteration 2" row is the only "Iteration 2" before selection.
  const loop = buildBlock({
    workflow_run_block_id: "wrb_loop",
    block_type: "for_loop",
    label: "checkout-loop",
    loop_values: ["alpha", "beta"],
    current_index: 0,
    current_value: "alpha",
    created_at: "2026-06-10T00:00:00Z",
    modified_at: "2026-06-10T00:01:00Z",
  });
  const iter0 = buildBlock({
    workflow_run_block_id: "wrb_iter0",
    block_type: "task",
    label: "iter0-task",
    parent_workflow_run_block_id: "wrb_loop",
    current_index: 0,
    created_at: "2026-06-10T00:00:10Z",
  });
  const iter1 = buildBlock({
    workflow_run_block_id: "wrb_iter1",
    block_type: "task",
    label: "iter1-task",
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

afterEach(() => {
  cleanup();
  mocks.workflowRun = undefined;
  mocks.timeline = undefined;
  mocks.runHeroProps = [];
});
beforeEach(() => useRunViewStore.getState().reset());

describe("RunView iteration selection", () => {
  test("selecting the loop block after an iteration clears the iteration scope", () => {
    seedForLoopRun();
    // Seed ?active= so the loop is the selected (and expanded) item on mount,
    // making its iteration rows visible.
    const { container } = render(
      <MemoryRouter initialEntries={["/?active=wrb_loop"]}>
        <RunView workflowRunId="wr_1" />
      </MemoryRouter>,
    );
    const scope = within(container);

    // Baseline: loop overview (all iterable values), not a single iteration.
    expect(scope.queryByText(/Iterable values/)).not.toBeNull();

    // Drill into iteration 2.
    fireEvent.click(scope.getByText("Iteration 2"));
    expect(scope.queryByText("Iteration 2 value")).not.toBeNull();
    expect(scope.queryByText(/Iterable values/)).toBeNull();

    // Click the loop block row (descriptor text is timeline-only). The detail
    // must fall back to the loop overview instead of staying on iteration 2.
    fireEvent.click(scope.getByText(/Loop over 2 values/));
    expect(scope.queryByText(/Iterable values/)).not.toBeNull();
    expect(scope.queryByText("Iteration 2 value")).toBeNull();
  });
});

describe("RunView screenshot center view", () => {
  test("selects the latest action frame when a running run opens Screenshots", () => {
    mocks.timeline = [
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_1",
          actions: [
            buildAction({
              action_id: "act_1",
              action_order: 0,
              screenshot_artifact_id: "art_1",
            }),
          ],
        }),
      ),
    ];
    mocks.workflowRun = {
      workflow_run_id: "wr_1",
      status: Status.Running,
      workflow: {
        workflow_definition: { blocks: [], finally_block_label: null },
      },
    };

    render(
      <MemoryRouter>
        <RunView workflowRunId="wr_1" />
      </MemoryRouter>,
    );

    const props = mocks.runHeroProps[mocks.runHeroProps.length - 1];
    expect(props?.heroSelection).toBeNull();

    act(() => {
      useRunViewStore.getState().pinFrame("stream");
      useRunViewStore.getState().setCenterView("screenshots");
    });

    const screenshotProps = mocks.runHeroProps[mocks.runHeroProps.length - 1];
    expect(screenshotProps?.hasScreenshots).toBe(true);
    expect(screenshotProps?.heroSelection).toMatchObject({
      kind: "action",
      artifactId: "art_1",
      stepId: "step_default",
      actionOrder: 0,
    });
  });

  test("does not advertise screenshots for actions without screenshot candidates", () => {
    mocks.timeline = [
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_1",
          actions: [
            buildAction({
              action_id: "act_1",
              step_id: null,
              screenshot_artifact_id: null,
            }),
          ],
        }),
      ),
    ];
    mocks.workflowRun = {
      workflow_run_id: "wr_1",
      status: Status.Completed,
      workflow: {
        workflow_definition: { blocks: [], finally_block_label: null },
      },
    };

    render(
      <MemoryRouter>
        <RunView workflowRunId="wr_1" />
      </MemoryRouter>,
    );

    const props = mocks.runHeroProps[mocks.runHeroProps.length - 1];
    expect(props?.hasScreenshots).toBe(false);
    expect(props?.heroSelection).toMatchObject({
      kind: "action",
      artifactId: null,
      stepId: null,
    });
  });

  test("does not advertise step screenshots without an action order", () => {
    mocks.timeline = [
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_1",
          actions: [
            buildAction({
              action_id: "act_1",
              action_order: null,
              screenshot_artifact_id: null,
            }),
          ],
        }),
      ),
    ];
    mocks.workflowRun = {
      workflow_run_id: "wr_1",
      status: Status.Completed,
      workflow: {
        workflow_definition: { blocks: [], finally_block_label: null },
      },
    };

    render(
      <MemoryRouter>
        <RunView workflowRunId="wr_1" />
      </MemoryRouter>,
    );

    const props = mocks.runHeroProps[mocks.runHeroProps.length - 1];
    expect(props?.hasScreenshots).toBe(false);
    expect(props?.heroSelection).toMatchObject({
      kind: "action",
      artifactId: null,
      stepId: "step_default",
      actionOrder: null,
    });
  });
});
