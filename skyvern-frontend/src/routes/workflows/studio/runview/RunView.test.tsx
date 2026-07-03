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
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import type {
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../../types/workflowRunTypes";
import { RunView } from "./RunView";
import { getSelectedRunFrameId } from "./runFrameSelection";

const mocks = vi.hoisted(() => ({
  workflowRun: undefined as unknown,
  timeline: undefined as unknown,
  debugSession: undefined as unknown,
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
  useDebugSessionQuery: () => ({ data: mocks.debugSession }),
}));
vi.mock("../../editor/hooks/useIsGeneratingCode", () => ({
  useIsGeneratingCode: () => false,
}));
vi.mock("./RunHero", () => ({
  RunHero: (props: Record<string, unknown> & { outputs?: ReactNode }) => {
    mocks.runHeroProps.push(props);
    return (
      <div data-testid="run-hero">
        {props.outputs ? (
          <div data-testid="run-outputs">{props.outputs}</div>
        ) : null}
      </div>
    );
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

function seedCompletedRun(overrides: Record<string, unknown> = {}) {
  mocks.timeline = [];
  mocks.workflowRun = {
    workflow_run_id: "wr_1",
    status: Status.Completed,
    downloaded_file_urls: [],
    downloaded_files: [],
    errors: null,
    outputs: null,
    workflow: {
      workflow_definition: { blocks: [], finally_block_label: null },
    },
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  mocks.workflowRun = undefined;
  mocks.timeline = undefined;
  mocks.debugSession = undefined;
  mocks.runHeroProps = [];
});
const initialStudioBrowserState = useStudioBrowserStore.getState();

// jsdom doesn't implement scrollIntoView; focusBrowserPane calls it via rAF.
Element.prototype.scrollIntoView = vi.fn();

beforeEach(() => {
  useRunViewStore.getState().reset();
  useStudioBrowserStore.setState(initialStudioBrowserState, true);
});

describe("getSelectedRunFrameId", () => {
  test("defaults to live stream while a run is running", () => {
    expect(
      getSelectedRunFrameId({
        pinnedFrameId: null,
        running: true,
        lastFrameId: "action_1",
      }),
    ).toBe("stream");
  });

  test("preserves an explicit pinned frame while a run is running", () => {
    expect(
      getSelectedRunFrameId({
        pinnedFrameId: "action_1",
        running: true,
        lastFrameId: "action_2",
      }),
    ).toBe("action_1");
  });

  test("uses the latest frame while a running run shows screenshots", () => {
    expect(
      getSelectedRunFrameId({
        pinnedFrameId: null,
        running: true,
        showingScreenshots: true,
        lastFrameId: "action_2",
      }),
    ).toBe("action_2");
  });

  test("uses the final frame after a live stream pin outlives a completed run", () => {
    expect(
      getSelectedRunFrameId({
        pinnedFrameId: "stream",
        running: false,
        lastFrameId: "action_2",
      }),
    ).toBe("action_2");
  });

  test("preserves an explicit pinned frame after a run finalizes", () => {
    expect(
      getSelectedRunFrameId({
        pinnedFrameId: "action_1",
        running: false,
        lastFrameId: "action_2",
      }),
    ).toBe("action_1");
  });

  test("defaults to the final frame after a run finalizes without a pin", () => {
    expect(
      getSelectedRunFrameId({
        pinnedFrameId: null,
        running: false,
        lastFrameId: "action_2",
      }),
    ).toBe("action_2");
  });

  test("uses the latest frame while the Browser pane owns a live debug stream", () => {
    expect(
      getSelectedRunFrameId({
        pinnedFrameId: null,
        running: true,
        debugStreamInBrowserPane: true,
        lastFrameId: "action_2",
      }),
    ).toBe("action_2");
  });
});

function seedLiveBlockRun() {
  mocks.debugSession = { browser_session_id: "pbs_1" };
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
    browser_session_id: "pbs_1",
    workflow: {
      workflow_definition: { blocks: [], finally_block_label: null },
    },
  };
}

describe("RunView block run and the Browser pane", () => {
  test("Browser pane open: the hero gets live-edge screenshots, not the stream selection", () => {
    seedLiveBlockRun();
    render(
      <MemoryRouter
        initialEntries={["/?wr=wr_1&bl=Block%201&panes=run,browser"]}
      >
        <RunView workflowRunId="wr_1" />
      </MemoryRouter>,
    );

    const props = mocks.runHeroProps[mocks.runHeroProps.length - 1];
    expect(props?.showDebugStream).toBe(true);
    expect(props?.debugStreamInBrowserPane).toBe(true);
    // Without the remap the "stream" selection would leave heroSelection null.
    expect(props?.heroSelection).toMatchObject({
      kind: "action",
      artifactId: "art_1",
    });
  });

  test("focusBrowserPane pins the Browser pane's Live view", () => {
    seedLiveBlockRun();
    useStudioBrowserStore.setState({ view: "screenshots" });
    render(
      <MemoryRouter
        initialEntries={["/?wr=wr_1&bl=Block%201&panes=run,browser"]}
      >
        <RunView workflowRunId="wr_1" />
      </MemoryRouter>,
    );

    const props = mocks.runHeroProps[mocks.runHeroProps.length - 1];
    act(() => {
      (props?.onFocusBrowserPane as () => void)();
    });
    // The CTA promises live; a pinned replay pill must not swallow it.
    expect(useStudioBrowserStore.getState().view).toBe("live");
  });

  test("Browser pane closed, Run pane open: the hero stays self-sufficient", () => {
    seedLiveBlockRun();
    render(
      <MemoryRouter initialEntries={["/?wr=wr_1&bl=Block%201&panes=run"]}>
        <RunView workflowRunId="wr_1" />
      </MemoryRouter>,
    );

    const props = mocks.runHeroProps[mocks.runHeroProps.length - 1];
    expect(props?.showDebugStream).toBe(true);
    expect(props?.debugStreamInBrowserPane).toBe(false);
    expect(props?.paneOpen).toBe(true);
  });
});

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
  }, 20_000);
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

describe("RunView output signals", () => {
  test("surfaces run errors, error codes, and rich downloaded files", () => {
    seedCompletedRun({
      errors: [
        {
          error_code: "E_INVOICE_MISSING",
          reasoning: "The expected invoice was not available.",
        },
        {
          error_code: "E_PAYMENT_BLOCKED",
          confidence_float: 0.91,
        },
      ],
      downloaded_files: [
        {
          url: "https://example.test/downloads/report.pdf",
          filename: "report.pdf",
          checksum: null,
          file_size: null,
          modified_at: null,
          artifact_id: null,
        },
      ],
      downloaded_file_urls: null,
    });

    const { container } = render(
      <MemoryRouter>
        <RunView workflowRunId="wr_1" />
      </MemoryRouter>,
    );
    const scope = within(container);

    expect(scope.getByText("Run errors")).not.toBeNull();
    expect(scope.getAllByText("E_INVOICE_MISSING").length).toBeGreaterThan(0);
    expect(scope.getByText("E_PAYMENT_BLOCKED")).not.toBeNull();
    expect(
      scope.getByText("The expected invoice was not available."),
    ).not.toBeNull();
    expect(scope.queryByText("confidence_float")).toBeNull();
    expect(scope.getByText("Downloaded files")).not.toBeNull();
    expect(scope.getByText("report.pdf")).not.toBeNull();
  });

  test("does not treat a user output parameter named errors as run errors", () => {
    seedCompletedRun({
      outputs: {
        errors: [{ message: "This is ordinary user output data." }],
      },
    });

    const { container } = render(
      <MemoryRouter>
        <RunView workflowRunId="wr_1" />
      </MemoryRouter>,
    );
    const scope = within(container);

    expect(scope.queryByText("Run errors")).toBeNull();
  });

  test("does not add an output slot when run signals are absent", () => {
    seedCompletedRun();

    const { container } = render(
      <MemoryRouter>
        <RunView workflowRunId="wr_1" />
      </MemoryRouter>,
    );
    const scope = within(container);

    expect(scope.queryByTestId("run-outputs")).toBeNull();
    expect(scope.queryByText("Run errors")).toBeNull();
    expect(scope.queryByText("Downloaded files")).toBeNull();
  });
});
