// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { type ReactNode } from "react";

import { ActionTypes, Status, type ActionsApiResponse } from "@/api/types";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useRunPaneViewStore } from "@/store/useRunPaneViewStore";
import { useRunViewStore } from "@/store/RunViewStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import { useWorkflowBlockSearchStore } from "@/store/WorkflowBlockSearchStore";
import type {
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../../types/workflowRunTypes";
import { StudioPaneCompactContext } from "../StudioShellContext";
import { RunPaneViewToggles } from "./RunPaneHeader";
import { RunView } from "./RunView";

const mocks = vi.hoisted(() => ({
  workflowRun: undefined as unknown,
  timeline: undefined as unknown,
  codeGenerating: false,
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
vi.mock("../../editor/hooks/useIsGeneratingCode", () => ({
  useIsGeneratingCode: () => mocks.codeGenerating,
}));
vi.mock("../../workflowRun/WorkflowRunCode", () => ({
  WorkflowRunCode: () => <div data-testid="workflow-run-code" />,
}));
vi.mock("../../workflowRun/WorkflowRunVerificationCodeForm", () => ({
  WorkflowRunVerificationCodeForm: () => null,
}));
vi.mock("@/routes/tasks/components/tagging/RunTagsEditor", () => ({
  RunTagsEditor: ({ workflowRunId }: { workflowRunId: string }) => (
    <div data-testid="run-tags-editor" data-workflow-run-id={workflowRunId} />
  ),
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
// The header toggles resolve the inspected run themselves; pin it to the same
// run the RunView under test renders (avoids the latest-run fallback query).
vi.mock("../useStudioInspectedRun", () => ({
  useStudioInspectedRun: () => ({
    runId: "wr_1",
    explicit: true,
    pending: false,
  }),
}));

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

function seedRunningRun() {
  mocks.timeline = [
    buildBlockItem(
      buildBlock({
        workflow_run_block_id: "wrb_1",
        label: "goto-block",
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

function LocationSpy() {
  const location = useLocation();
  return <div data-testid="location-search">{location.search}</div>;
}

function renderRunView(
  props: Partial<Parameters<typeof RunView>[0]> = {},
  initialEntry = "/",
  compact = false,
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  // Fresh elements per (re)render so React re-runs the mocked hooks; the
  // component instances (and the MemoryRouter's URL state) are preserved.
  const makeUi = () => (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        {/* The toggles live in the pane header (StudioShell); render them
            alongside the body, under a TooltipProvider, the way the shell
            composes them. Only headerExtras (the toggles) sit under the
            compact context in production (StudioShell.tsx), not the body. */}
        <TooltipProvider delayDuration={0}>
          <StudioPaneCompactContext.Provider value={compact}>
            <RunPaneViewToggles />
          </StudioPaneCompactContext.Provider>
          <RunView workflowRunId="wr_1" {...props} />
        </TooltipProvider>
        <LocationSpy />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(makeUi());
  return { ...view, rerenderRunView: () => view.rerender(makeUi()) };
}

afterEach(() => {
  cleanup();
  mocks.workflowRun = undefined;
  mocks.timeline = undefined;
  mocks.codeGenerating = false;
});
beforeEach(() => {
  useRunViewStore.getState().reset();
  useRunPaneViewStore.getState().reset();
  useStudioBrowserStore.setState({ view: "auto" });
});

describe("RunView view toggles", () => {
  test("does not render run tags in Studio Overview", () => {
    seedCompletedRun();
    const { queryByTestId } = renderRunView();

    expect(queryByTestId("run-tags-editor")).toBeNull();
  });

  test("defaults to the Timeline view with the timeline and step detail", () => {
    seedForLoopRun();
    const { container } = renderRunView();
    const scope = within(container);

    expect(scope.getByRole("group", { name: "Run view" })).not.toBeNull();
    // The timeline tree is visible by default (loop row present).
    expect(scope.queryAllByText("checkout-loop").length).toBeGreaterThan(0);
  });

  test("the Timeline view leads with the summary meta line", () => {
    seedCompletedRun({
      total_steps: 12,
      credits_used: 3,
      cached_credits_used: 2,
    });
    mocks.timeline = [
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_1",
          actions: [buildAction({ action_id: "act_1" })],
        }),
      ),
    ];
    const { container } = renderRunView();
    const scope = within(container);

    // status · duration · run id — the counts live in the timeline's own
    // header row, so the strip carries no stat boxes.
    expect(scope.getByText("wr_1")).not.toBeNull();
    expect(
      scope.getAllByText("completed", { exact: false }).length,
    ).toBeGreaterThan(0);
    expect(scope.queryByText("Steps")).toBeNull();
    expect(scope.queryByText("Credits")).toBeNull();
  });

  test("Inputs view shows the run's input metadata", () => {
    seedCompletedRun({
      webhook_callback_url: "https://example.test/hook",
    });
    const { container } = renderRunView();
    const scope = within(container);

    fireEvent.click(scope.getByRole("button", { name: "Inputs" }));
    expect(scope.getByText("Webhook URL")).not.toBeNull();
    expect(scope.getByText("https://example.test/hook")).not.toBeNull();
  });

  test("Code view renders the shared WorkflowRunCode surface", () => {
    seedCompletedRun();
    const { container } = renderRunView();
    const scope = within(container);

    expect(scope.queryByTestId("workflow-run-code")).toBeNull();
    fireEvent.click(scope.getByRole("button", { name: "Code" }));
    expect(scope.queryByTestId("workflow-run-code")).not.toBeNull();
  });

  test("the Code toggle shows a spinner while cached code is generating", () => {
    seedCompletedRun();
    mocks.codeGenerating = true;
    const { container } = renderRunView();
    const scope = within(container);

    expect(scope.queryByTestId("code-generating-spinner")).not.toBeNull();
  });

  test("Inputs and Outputs stay visible without data and show empty states", () => {
    seedCompletedRun();
    const { container } = renderRunView();
    const scope = within(container);

    fireEvent.click(scope.getByRole("button", { name: "Inputs" }));
    expect(scope.getByText("No inputs for this run")).not.toBeNull();

    fireEvent.click(scope.getByRole("button", { name: "Outputs" }));
    expect(scope.getByText("No outputs for this run")).not.toBeNull();
  });

  test("definition block prompts count as run inputs", () => {
    seedCompletedRun({
      workflow: {
        workflow_definition: {
          blocks: [
            {
              block_type: "navigation",
              label: "navigation block",
              navigation_goal: "Navigate to the next synthetic step",
            },
          ],
          finally_block_label: null,
        },
      },
    });
    const { container } = renderRunView();
    const scope = within(container);

    fireEvent.click(scope.getByRole("button", { name: "Inputs" }));

    expect(scope.queryByText("navigation block")).not.toBeNull();
    expect(
      scope.queryByText("Navigate to the next synthetic step"),
    ).not.toBeNull();
    expect(scope.queryByText("No inputs for this run")).toBeNull();
  });
});

describe("RunView cold-open selection", () => {
  function seedTerminalRunWithActions() {
    mocks.timeline = [
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_1",
          label: "goto-block",
          // Newest-first, matching the API; the filmstrip reverses per block.
          actions: [
            buildAction({ action_id: "act_2", action_order: 1 }),
            buildAction({ action_id: "act_1", action_order: 0 }),
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
  }

  test("a terminal ?wr= deep link with no ?active= selects the last item", () => {
    seedTerminalRunWithActions();
    const { getByTestId } = renderRunView({}, "/?wr=wr_1");

    expect(useRunViewStore.getState().pinnedFrameId).toBe("act_2");
    expect(getByTestId("location-search").textContent).toContain("active=");
  });

  test("an explicit ?active= deep link wins over the last-item default", () => {
    seedTerminalRunWithActions();
    renderRunView({}, "/?wr=wr_1&active=act_1");

    expect(useRunViewStore.getState().pinnedFrameId).toBe("act_1");
  });

  test("a still-running run keeps following the live edge", () => {
    seedRunningRun();
    renderRunView({}, "/?wr=wr_1");

    expect(useRunViewStore.getState().pinnedFrameId).toBeNull();
  });

  test("a block-iterate link (?bl=) keeps its live surface unselected", () => {
    seedTerminalRunWithActions();
    renderRunView({}, "/?wr=wr_1&bl=goto-block");

    expect(useRunViewStore.getState().pinnedFrameId).toBeNull();
  });
});

describe("RunView live-watch terminal transition", () => {
  function seedWatchedRun(status: Status) {
    mocks.timeline = [
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_1",
          label: "goto-block",
          // Newest-first, matching the API; the filmstrip reverses per block.
          actions: [
            buildAction({ action_id: "act_2", action_order: 1 }),
            buildAction({ action_id: "act_1", action_order: 0 }),
          ],
        }),
      ),
    ];
    mocks.workflowRun = {
      workflow_run_id: "wr_1",
      status,
      browser_session_id: "pbs_1",
      workflow: {
        workflow_definition: { blocks: [], finally_block_label: null },
      },
    };
  }

  test("a watched run finishing lands the selection on the last item", () => {
    seedWatchedRun(Status.Running);
    const view = renderRunView({}, "/?wr=wr_1");
    expect(useRunViewStore.getState().pinnedFrameId).toBeNull();

    seedWatchedRun(Status.Completed);
    view.rerenderRunView();

    // The pin + ?active= hand the Browser pane's auto view to the machine,
    // which resolves scrubbing to Screenshots on the final item.
    expect(useRunViewStore.getState().pinnedFrameId).toBe("act_2");
    expect(view.getByTestId("location-search").textContent).toContain(
      "active=act_2",
    );
    expect(useStudioBrowserStore.getState().view).toBe("auto");
  });

  test("a view pill pinned mid-watch is never overridden at run end", () => {
    seedWatchedRun(Status.Running);
    const view = renderRunView({}, "/?wr=wr_1");
    useStudioBrowserStore.getState().setView("recording");

    seedWatchedRun(Status.Completed);
    view.rerenderRunView();

    expect(useRunViewStore.getState().pinnedFrameId).toBeNull();
    expect(useStudioBrowserStore.getState().view).toBe("recording");
  });

  test("a timeline pin made mid-watch is never overridden at run end", () => {
    seedWatchedRun(Status.Running);
    const view = renderRunView({}, "/?wr=wr_1");
    useRunViewStore.getState().pinFrame("act_1");

    seedWatchedRun(Status.Completed);
    view.rerenderRunView();

    expect(useRunViewStore.getState().pinnedFrameId).toBe("act_1");
  });
});

describe("RunView failure banner", () => {
  test("shows the failure reason with working Fix and Retry CTAs", () => {
    seedCompletedRun({
      status: Status.Failed,
      failure_reason: "Login page rejected the credentials",
    });
    const onFix = vi.fn();
    const onRetry = vi.fn();
    const { container } = renderRunView({ onFix, onRetry });
    const scope = within(container);

    expect(
      scope.getByText("Login page rejected the credentials"),
    ).not.toBeNull();

    fireEvent.click(scope.getByRole("button", { name: "Fix with Copilot" }));
    expect(onFix).toHaveBeenCalledTimes(1);
    expect(onFix.mock.calls[0]?.[0]).toContain(
      "Login page rejected the credentials",
    );

    fireEvent.click(scope.getByRole("button", { name: "Retry as-is" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  test("dismiss hides the failure banner", () => {
    seedCompletedRun({
      status: Status.Failed,
      failure_reason: "Something broke",
    });
    const { container } = renderRunView();
    const scope = within(container);

    fireEvent.click(scope.getByRole("button", { name: "Dismiss" }));
    expect(scope.queryByText("Something broke")).toBeNull();
  });

  test("no failure banner for a user-canceled run", () => {
    seedCompletedRun({
      status: Status.Canceled,
      failure_reason: "canceled by user",
    });
    const { container } = renderRunView();
    const scope = within(container);

    expect(scope.queryByText("canceled by user")).toBeNull();
  });
});

describe("RunView live affordances", () => {
  test("a running run shows the Live chip; clicking hands off to the Browser pane", () => {
    seedRunningRun();
    useRunViewStore.getState().pinFrame("act_1");
    // Park the Browser pane on a pinned replay view: the Live CTA promises
    // live, so the pinned pill must not swallow the handoff.
    useStudioBrowserStore.setState({ view: "screenshots" });
    const { container, getByTestId } = renderRunView();
    const scope = within(container);

    fireEvent.click(scope.getByRole("button", { name: "Live" }));

    // Unpins to the live edge and pins the Browser pane's view intent to live.
    expect(useRunViewStore.getState().pinnedFrameId).toBeNull();
    expect(useStudioBrowserStore.getState().view).toBe("live");
    expect(getByTestId("location-search").textContent).toContain("browser");
  });

  test("a queued run shows the queued chip instead of the Live chip", () => {
    seedCompletedRun({ status: Status.Queued });
    const { container } = renderRunView();
    const scope = within(container);

    expect(scope.queryByText(/Run queued/)).not.toBeNull();
    expect(scope.queryByRole("button", { name: "Live" })).toBeNull();
  });
});

describe("RunView iteration selection", () => {
  test("selecting the loop block after an iteration clears the iteration scope", () => {
    seedForLoopRun();
    // Seed ?active= so the loop is the selected (and expanded) item on mount,
    // making its iteration rows visible.
    const { container } = renderRunView({}, "/?active=wrb_loop");
    const scope = within(container);

    // Baseline: loop overview (all iterable values), not a single iteration.
    expect(scope.queryByText(/Iterable values/)).not.toBeNull();

    // Drill into iteration 2.
    fireEvent.click(scope.getByText("Iteration 2"));
    expect(scope.queryByText("Iteration 2 value")).not.toBeNull();
    expect(scope.queryByText(/Iterable values/)).toBeNull();
    // The iteration scope is shared with the Browser pane via the store.
    expect(useRunViewStore.getState().activeIteration).toBe(1);

    // Click the loop block row (descriptor text is timeline-only). The detail
    // must fall back to the loop overview instead of staying on iteration 2.
    fireEvent.click(scope.getByText(/Loop over 2 values/));
    expect(scope.queryByText(/Iterable values/)).not.toBeNull();
    expect(scope.queryByText("Iteration 2 value")).toBeNull();
    expect(useRunViewStore.getState().activeIteration).toBeNull();
  }, 20_000);
});

describe("RunView timeline → editor jump", () => {
  function seedRunWithBlock(label: string) {
    mocks.timeline = [
      buildBlockItem(buildBlock({ workflow_run_block_id: "wrb_jump", label })),
    ];
    mocks.workflowRun = {
      workflow_run_id: "wr_1",
      status: Status.Completed,
      workflow: {
        workflow_definition: { blocks: [], finally_block_label: null },
      },
    };
  }

  function registerHandle() {
    const focusBlock = vi.fn();
    useWorkflowBlockSearchStore.getState().registerHandle({
      getTargets: () => [
        { nodeId: "node-jump", label: "jump-target-block", blockType: null },
      ],
      focusBlock,
    });
    return focusBlock;
  }

  function clickBlock(container: HTMLElement) {
    const [blockButton] = within(container).getAllByText("jump-target-block");
    if (!blockButton) {
      throw new Error("timeline block did not render");
    }
    fireEvent.click(blockButton);
  }

  afterEach(() => {
    useWorkflowBlockSearchStore.getState().registerHandle(null);
  });

  test("clicking a timeline block jumps the editor when the editor pane is open", () => {
    seedRunWithBlock("jump-target-block");
    const focusBlock = registerHandle();

    const { container } = renderRunView({}, "/?wr=wr_1&panes=editor,overview");
    clickBlock(container);

    expect(focusBlock).toHaveBeenCalledWith("node-jump");
  });

  test("clicking a timeline block does not jump when the editor pane is closed", () => {
    seedRunWithBlock("jump-target-block");
    const focusBlock = registerHandle();

    const { container } = renderRunView({}, "/?wr=wr_1&panes=overview");
    clickBlock(container);

    expect(focusBlock).not.toHaveBeenCalled();
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

    const { container } = renderRunView();
    const scope = within(container);

    // This run has outputs, so the toggle's accessible name carries the new-
    // output indicator suffix; match by prefix since that's not under test here.
    fireEvent.click(scope.getByRole("button", { name: /^Outputs/ }));

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

  test("surfaces the full agent run outputs below extracted information", () => {
    seedCompletedRun({
      outputs: {
        extracted_information: { answer: 42 },
        additional_output: "full-run-only",
      },
    });

    const { container } = renderRunView();
    const scope = within(container);

    // This run has outputs, so the toggle name carries the new-output suffix.
    fireEvent.click(scope.getByRole("button", { name: /^Outputs/ }));

    expect(scope.getByText("Extracted information")).not.toBeNull();
    expect(scope.getByText("Agent run outputs")).not.toBeNull();
  });

  test("does not treat a user output parameter named errors as run errors", () => {
    seedCompletedRun({
      outputs: {
        errors: [{ message: "This is ordinary user output data." }],
      },
    });

    const { container } = renderRunView();
    const scope = within(container);

    fireEvent.click(scope.getByRole("button", { name: "Outputs" }));
    expect(scope.queryByText("Run errors")).toBeNull();
  });

  test("shows the Outputs empty state when run signals are absent", () => {
    seedCompletedRun();

    const { container } = renderRunView();
    const scope = within(container);

    fireEvent.click(scope.getByRole("button", { name: "Outputs" }));
    expect(scope.getByText("No outputs for this run")).not.toBeNull();
    expect(scope.queryByText("Run errors")).toBeNull();
    expect(scope.queryByText("Downloaded files")).toBeNull();
  });
});

describe("RunView output indicator", () => {
  test("the Outputs toggle carries a new-output indicator when unviewed output exists", () => {
    seedCompletedRun({ errors: [{ error_code: "E1", reasoning: "x" }] });
    const { container } = renderRunView();
    const scope = within(container);

    expect(
      scope.getByRole("button", { name: "Outputs, content available" }),
    ).not.toBeNull();
  });

  test("the indicator clears once the Outputs view is active", () => {
    seedCompletedRun({ errors: [{ error_code: "E1", reasoning: "x" }] });
    const { container } = renderRunView();
    const scope = within(container);

    fireEvent.click(
      scope.getByRole("button", { name: "Outputs, content available" }),
    );
    expect(scope.getByRole("button", { name: "Outputs" })).not.toBeNull();
    expect(
      scope.queryByRole("button", { name: "Outputs, content available" }),
    ).toBeNull();
  });

  test("no indicator when the run has no output signals", () => {
    seedCompletedRun();
    const { container } = renderRunView();
    const scope = within(container);

    expect(scope.getByRole("button", { name: "Outputs" })).not.toBeNull();
    expect(
      scope.queryByRole("button", { name: "Outputs, content available" }),
    ).toBeNull();
  });

  test("the indicator survives the header collapsing to icon-only", () => {
    seedCompletedRun({ errors: [{ error_code: "E1", reasoning: "x" }] });
    const { container } = renderRunView({}, "/", true);
    const scope = within(container);

    expect(
      scope.getByRole("button", { name: "Outputs, content available" }),
    ).not.toBeNull();
  });
});
