// @vitest-environment jsdom

import {
  QueryClient,
  QueryClientProvider,
  useQuery,
} from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
  useSearchParams,
} from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { type ReactNode } from "react";

import {
  ActionTypes,
  ArtifactType,
  Status,
  type ActionsApiResponse,
} from "@/api/types";
import { TooltipProvider } from "@/components/ui/tooltip";
import { WorkflowPermanentIdContext } from "@/routes/workflows/WorkflowPermanentIdContext";
import { PageSlotsProvider, type PageSlots } from "@/store/PageSlots";
import { useRunPaneViewStore } from "@/store/useRunPaneViewStore";
import { useRunViewStore } from "@/store/RunViewStore";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";
import { useWorkflowBlockSearchStore } from "@/store/WorkflowBlockSearchStore";
import type {
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../../types/workflowRunTypes";
import { WorkflowCopilotChat } from "../../copilot/WorkflowCopilotChat";
import { StudioPaneCompactContext } from "../StudioShellContext";
import { RunPaneViewToggles } from "./RunPaneHeader";
import { RunView } from "./RunView";

const mocks = vi.hoisted(() => ({
  workflowRun: undefined as unknown,
  timeline: undefined as unknown,
  codeGenerating: false,
  isPlaceholderData: false,
  statusUnavailable: false,
  refetchRunStatus: vi.fn(),
}));
const { getSpy } = vi.hoisted(() => ({ getSpy: vi.fn() }));

vi.mock("@/api/AxiosClient", () => ({
  getClient: async () => ({ get: getSpy }),
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => undefined,
}));

vi.mock("../../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => ({
    data: mocks.workflowRun,
    isLoading: false,
    isPlaceholderData: mocks.isPlaceholderData,
    isError: mocks.statusUnavailable,
    refetch: mocks.refetchRunStatus,
  }),
}));
vi.mock("../../hooks/useWorkflowRunTimelineQuery", () => ({
  useWorkflowRunTimelineQuery: () => ({
    data: mocks.timeline,
    isLoading: false,
    isPlaceholderData: mocks.isPlaceholderData,
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
// The header's "…" menu (Radix DropdownMenu) scrolls its focused item into
// view on open; jsdom implements neither that nor ResizeObserver.
Element.prototype.scrollIntoView = () => {};
Element.prototype.scrollTo = () => {};
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}
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

function sealedSavedRunReplayOverrides(): Record<string, unknown> {
  const runId = process.env.SAVED_RUN_REPLAY_RUN_ID;
  if (runId === undefined) {
    return {};
  }
  expect(runId).toBe("wr_568303252034165152");
  expect(process.env.SAVED_RUN_REPLAY_TERMINAL_STATUS).toBe(Status.Completed);
  expect(process.env.SAVED_RUN_REPLAY_BLOCK_OUTPUT).toBe("null");
  expect(process.env.SAVED_RUN_REPLAY_MISSING_FIELD).toBe(
    "workflow_run_block_output",
  );
  return {
    workflow_run_id: runId,
    status: process.env.SAVED_RUN_REPLAY_TERMINAL_STATUS,
    outputs: null,
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

// Subscribes to the failure card's artifact query so a test can wait for its
// result to reach the DOM instead of for the request to start.
function ArtifactsQueryProbe({
  workflowRunBlockId,
}: {
  workflowRunBlockId: string;
}) {
  const { status } = useQuery({
    queryKey: ["workflowRunBlock", workflowRunBlockId, "artifacts"],
    enabled: false,
  });
  return <div data-testid="artifacts-query">{status}</div>;
}

// Stands in for the editor canvas: selecting a block there mirrors the label
// into ?selected-block= (useSelectedBlockUrlSync).
function SelectBlockOnCanvas({ label }: { label: string }) {
  const [params, setParams] = useSearchParams();
  return (
    <button
      onClick={() => {
        const next = new URLSearchParams(params);
        next.set("selected-block", label);
        setParams(next, { replace: true });
      }}
    >
      canvas: select {label}
    </button>
  );
}

function ReleaseCopilotRunFocus() {
  const [params, setParams] = useSearchParams();
  return (
    <button
      onClick={() => {
        const next = new URLSearchParams(params);
        next.delete("wr");
        next.delete("wrs");
        setParams(next, { replace: true });
      }}
    >
      release copilot focus
    </button>
  );
}

function renderRunView(
  props: Partial<Parameters<typeof RunView>[0]> = {},
  initialEntry = "/",
  compact = false,
  extra?: ReactNode,
  pageSlots: PageSlots = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  // Fresh elements per (re)render so React re-runs the mocked hooks; the
  // component instances (and the MemoryRouter's URL state) are preserved.
  const makeUi = () => (
    <QueryClientProvider client={queryClient}>
      <PageSlotsProvider value={pageSlots}>
        <MemoryRouter initialEntries={[initialEntry]}>
          {/* The toggles live in the pane header (StudioShell); render them
              alongside the body, under a TooltipProvider, the way the shell
              composes them. Only headerExtras (the toggles) sit under the
              compact context in production (StudioShell.tsx), not the body. */}
          <TooltipProvider delayDuration={0}>
            <StudioPaneCompactContext.Provider value={compact}>
              <RunPaneViewToggles />
            </StudioPaneCompactContext.Provider>
            {initialEntry.startsWith("/runs/") ? (
              <Routes>
                <Route
                  path="/runs/:runId"
                  element={<RunView workflowRunId="wr_1" {...props} />}
                />
              </Routes>
            ) : (
              <RunView workflowRunId="wr_1" {...props} />
            )}
          </TooltipProvider>
          <LocationSpy />
          {extra}
        </MemoryRouter>
      </PageSlotsProvider>
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
  mocks.isPlaceholderData = false;
  mocks.statusUnavailable = false;
  mocks.refetchRunStatus.mockReset();
});
beforeEach(() => {
  getSpy.mockReset();
  getSpy.mockResolvedValue({ data: [] });
  useRunViewStore.getState().reset();
  useRunPaneViewStore.getState().reset();
  useStudioBrowserStore.setState({ view: "auto" });
});

describe("RunView view toggles", () => {
  test("mounts the milestone slot throughout Overview but not the editor", () => {
    seedCompletedRun();
    const MilestoneCard = vi.fn(() => <div data-testid="milestone-card" />);
    const pageSlots = { workflowRunMilestoneCard: MilestoneCard };

    const editor = renderRunView(
      {},
      "/?panes=editor",
      false,
      undefined,
      pageSlots,
    );
    expect(MilestoneCard).not.toHaveBeenCalled();
    expect(editor.queryByTestId("milestone-card")).toBeNull();
    cleanup();

    const overview = renderRunView(
      {},
      "/?panes=overview",
      false,
      undefined,
      pageSlots,
    );
    expect(overview.queryByTestId("milestone-card")).not.toBeNull();
    for (const view of ["inputs", "outputs", "code"] as const) {
      act(() => useRunPaneViewStore.getState().setView(view));
      overview.rerenderRunView();
      expect(overview.queryByTestId("milestone-card")).not.toBeNull();
    }
  });

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
    // Studio opts into label search (legacy run view does not).
    expect(scope.getByRole("button", { name: "Search blocks" })).not.toBeNull();
  });

  test("filters the Studio timeline search by top-level block label", () => {
    seedCompletedRun();
    const login = buildBlock({
      workflow_run_block_id: "wrb_login",
      label: "Login",
      created_at: "2026-01-01T00:00:00Z",
    });
    const extract = buildBlock({
      workflow_run_block_id: "wrb_extract",
      label: "Extract rows",
      created_at: "2026-01-01T00:01:00Z",
    });
    const loop = buildBlock({
      workflow_run_block_id: "wrb_loop",
      block_type: "for_loop",
      label: "checkout_loop",
      created_at: "2026-01-01T00:02:00Z",
    });
    const nested = buildBlock({
      workflow_run_block_id: "wrb_nested",
      label: "inner_step",
      parent_workflow_run_block_id: "wrb_loop",
      created_at: "2026-01-01T00:03:00Z",
    });
    mocks.timeline = [
      buildBlockItem(login),
      buildBlockItem(extract),
      buildBlockItem(loop, [buildBlockItem(nested)]),
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_unlabeled",
          label: null,
          created_at: "2026-01-01T00:04:00Z",
        }),
      ),
    ];

    const { container } = renderRunView();
    const scope = within(container);
    fireEvent.click(scope.getByRole("button", { name: "Search blocks" }));

    expect(screen.getAllByRole("option")).toHaveLength(3);
    fireEvent.change(screen.getByPlaceholderText("Search blocks…"), {
      target: { value: "ROWS" },
    });

    const options = screen.getAllByRole("option");
    expect(options).toHaveLength(1);
    expect(options[0]?.textContent).toContain("Extract rows");
  });

  test("selecting a Studio timeline search result pins its block", () => {
    seedCompletedRun();
    mocks.timeline = [
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_extract",
          label: "Extract rows",
        }),
      ),
    ];

    const { container } = renderRunView();
    const scope = within(container);
    fireEvent.click(scope.getByRole("button", { name: "Search blocks" }));
    fireEvent.click(screen.getByRole("option", { name: /Extract rows/ }));

    expect(useRunViewStore.getState().pinnedFrameId).toBe("wrb_extract");
    expect(screen.queryByPlaceholderText("Search blocks…")).toBeNull();
  });

  test("keeps Escape in the Studio block search", () => {
    seedCompletedRun();
    mocks.timeline = [buildBlockItem(buildBlock({ label: "Login" }))];
    const windowEscape = vi.fn();
    window.addEventListener("keydown", windowEscape);
    try {
      const { container } = renderRunView();
      const scope = within(container);
      fireEvent.click(scope.getByRole("button", { name: "Search blocks" }));
      fireEvent.keyDown(screen.getByPlaceholderText("Search blocks…"), {
        key: "Escape",
      });

      expect(screen.queryByPlaceholderText("Search blocks…")).toBeNull();
      expect(windowEscape).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener("keydown", windowEscape);
    }
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

    // status · duration · counts · search on one line — the run id lives in
    // the top bar's "View Run" tab, so the strip carries no id chip, and the
    // timeline below renders no title row of its own.
    expect(scope.queryByText("wr_1")).toBeNull();
    expect(
      scope.getAllByText("completed", { exact: false }).length,
    ).toBeGreaterThan(0);
    expect(scope.queryByText("Steps")).toBeNull();
    expect(scope.queryByText("Credits")).toBeNull();
    // The only "Timeline" left is the pane header's view pill; the list no
    // longer paints a title row of its own.
    expect(
      scope.getAllByText("Timeline").every((node) => node.closest("button")),
    ).toBe(true);
    const strip = scope.getByRole("button", { name: "Search blocks" })
      .parentElement?.parentElement;
    expect(strip).not.toBeNull();
    expect(within(strip as HTMLElement).getByText("credits")).toBeTruthy();
    expect(within(strip as HTMLElement).getByText("5")).toBeTruthy();
    expect(within(strip as HTMLElement).getByText("block")).toBeTruthy();
  });

  test("Inputs view shows the run's input metadata, including TOTP diagnostics", () => {
    seedCompletedRun({
      webhook_callback_url: "https://example.test/hook",
      totp_verification_url: "https://example.test/totp",
      totp_identifier: "totp-identifier-1",
    });
    const { container } = renderRunView();
    const scope = within(container);

    fireEvent.click(scope.getByRole("button", { name: "Inputs" }));
    expect(scope.getByText("Webhook URL")).not.toBeNull();
    expect(scope.getByText("https://example.test/hook")).not.toBeNull();
    expect(scope.getByText("TOTP URL")).not.toBeNull();
    expect(scope.getByText("https://example.test/totp")).not.toBeNull();
    expect(scope.getByText("TOTP identifier")).not.toBeNull();
    expect(scope.getByText("totp-identifier-1")).not.toBeNull();
  });

  test("browser session/profile ids live in the Inputs view, not the Timeline strip", () => {
    seedCompletedRun({
      browser_session_id: "pbs_1",
      browser_profile_id: "bp_1",
    });
    const { container } = renderRunView();
    const scope = within(container);

    expect(scope.queryByText("pbs_1")).toBeNull();
    expect(scope.queryByText("bp_1")).toBeNull();

    fireEvent.click(scope.getByRole("button", { name: "Inputs" }));
    expect(scope.getByText("Browser session")).not.toBeNull();
    expect(
      scope.getByRole("link", { name: "pbs_1" }).getAttribute("href"),
    ).toBe("/browser-session/pbs_1/stream");
    expect(scope.getByText("Browser profile")).not.toBeNull();
    expect(scope.getByRole("link", { name: "bp_1" }).getAttribute("href")).toBe(
      "/browser-profiles/bp_1",
    );
    expect(
      scope.getAllByRole("button", { name: "Copy to clipboard" }),
    ).toHaveLength(2);
  });

  test("a session-only run lists just the browser session in Inputs", () => {
    seedCompletedRun({ browser_session_id: "pbs_only" });
    const { container } = renderRunView();
    const scope = within(container);

    fireEvent.click(scope.getByRole("button", { name: "Inputs" }));
    expect(
      scope.getByRole("link", { name: "pbs_only" }).getAttribute("href"),
    ).toBe("/browser-session/pbs_only/stream");
    expect(scope.queryByText("Browser profile")).toBeNull();
  });

  test("Inputs view sources TOTP from task_v2 when the top-level run omits it", () => {
    seedCompletedRun({
      totp_verification_url: null,
      totp_identifier: null,
      task_v2: {
        totp_verification_url: "https://example.test/totp-v2",
        totp_identifier: "totp-identifier-v2",
      },
    });
    const { container } = renderRunView();
    const scope = within(container);

    fireEvent.click(scope.getByRole("button", { name: "Inputs" }));
    expect(scope.getByText("TOTP URL")).not.toBeNull();
    expect(scope.getByText("https://example.test/totp-v2")).not.toBeNull();
    expect(scope.getByText("TOTP identifier")).not.toBeNull();
    expect(scope.getByText("totp-identifier-v2")).not.toBeNull();
  });

  test("Inputs view omits TOTP rows when the run carries no TOTP config", () => {
    seedCompletedRun({
      webhook_callback_url: "https://example.test/hook",
    });
    const { container } = renderRunView();
    const scope = within(container);

    fireEvent.click(scope.getByRole("button", { name: "Inputs" }));
    expect(scope.getByText("Webhook URL")).not.toBeNull();
    expect(scope.queryByText("TOTP URL")).toBeNull();
    expect(scope.queryByText("TOTP identifier")).toBeNull();
  });

  test("the '…' menu's Code item renders the shared WorkflowRunCode surface", async () => {
    seedCompletedRun();
    const { container } = renderRunView();
    const scope = within(container);

    expect(scope.queryByTestId("workflow-run-code")).toBeNull();
    // The Code view lives in the header's "…" overflow menu, not a toggle.
    fireEvent.pointerDown(scope.getByRole("button", { name: "More views" }), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Code" }));
    expect(scope.queryByTestId("workflow-run-code")).not.toBeNull();
    // The menu trigger exposes aria-pressed while Code is the active view,
    // matching the sibling view toggles.
    expect(
      scope.getByRole("button", { name: "More views", pressed: true }),
    ).not.toBeNull();
  });

  test("the '…' trigger shows a spinner while cached code is generating", () => {
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

  test("a terminal /runs/{wr} short link with no ?active= selects the last item", () => {
    seedTerminalRunWithActions();
    renderRunView({}, "/runs/wr_1");

    expect(useRunViewStore.getState().pinnedFrameId).toBe("act_2");
  });

  test("an explicit ?active= deep link wins over the last-item default", () => {
    seedTerminalRunWithActions();
    renderRunView({}, "/?wr=wr_1&active=act_1");

    expect(useRunViewStore.getState().pinnedFrameId).toBe("act_1");
  });

  test("a terminal Copilot-focused run stays unselected through release", () => {
    seedTerminalRunWithActions();
    const view = renderRunView(
      {},
      "/?wr=wr_1&wrs=copilot",
      false,
      <ReleaseCopilotRunFocus />,
    );

    expect(useRunViewStore.getState().pinnedFrameId).toBeNull();
    expect(view.getByTestId("location-search").textContent).not.toContain(
      "active=",
    );

    fireEvent.click(
      view.getByRole("button", { name: "release copilot focus" }),
    );

    expect(useRunViewStore.getState().pinnedFrameId).toBeNull();
    expect(view.getByTestId("location-search").textContent).not.toContain(
      "active=",
    );
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

  test("does not auto-pin from the previous run's placeholder frames on a run switch", () => {
    // Mid-switch to wr_2: keepPreviousData still serves the OLD run's finalized
    // data + frames, flagged placeholder. Auto-pin must wait for wr_2's real
    // payload rather than lock this run's one-shot to the stale last frame.
    seedTerminalRunWithActions();
    mocks.isPlaceholderData = true;
    const view = renderRunView({ workflowRunId: "wr_2" }, "/?wr=wr_2");

    expect(useRunViewStore.getState().pinnedFrameId).toBeNull();

    // Real data arrives (no longer placeholder): now the one-shot decides.
    mocks.isPlaceholderData = false;
    view.rerenderRunView();

    expect(useRunViewStore.getState().pinnedFrameId).toBe("act_2");
  });
});

describe("RunView canvas selection sync", () => {
  function seedTwoBlockRun() {
    mocks.timeline = [
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_login",
          label: "login",
          actions: [buildAction({ action_id: "act_login" })],
        }),
      ),
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_checkout",
          label: "checkout",
          actions: [buildAction({ action_id: "act_checkout" })],
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

  test("selecting a block on the canvas moves ?active= onto that block", () => {
    // ?active= is what the Browser pane follows (useRunVisuals), so this is the
    // whole canvas → run → screenshot chain, not just the store write.
    seedTwoBlockRun();
    const { getByTestId, getByRole } = renderRunView(
      {},
      "/?wr=wr_1&panes=editor,overview",
      false,
      <SelectBlockOnCanvas label="login" />,
    );
    // Cold open auto-pins the last item, in the OTHER block.
    expect(getByTestId("location-search").textContent).not.toContain(
      "active=wrb_login",
    );

    fireEvent.click(getByRole("button", { name: /canvas: select login/ }));

    expect(useRunViewStore.getState().pinnedFrameId).toBe("wrb_login");
    expect(getByTestId("location-search").textContent).toContain(
      "active=wrb_login",
    );
  });

  test("leaves the run selection alone while the editor pane is closed", () => {
    seedTwoBlockRun();
    const { getByTestId, getByRole } = renderRunView(
      {},
      "/?wr=wr_1&panes=overview",
      false,
      <SelectBlockOnCanvas label="login" />,
    );

    fireEvent.click(getByRole("button", { name: /canvas: select login/ }));

    expect(getByTestId("location-search").textContent).not.toContain(
      "active=wrb_login",
    );
  });

  test("authoring a block writes no run reference while the Run pane is closed", () => {
    // The edit layout: no run in the URL, Run pane closed, RunView still mounted.
    // Writing ?active= here would make the search run-class and open the run
    // surfaces, so one click on the canvas dropped the user into the last run.
    seedTwoBlockRun();
    const { getByTestId, getByRole } = renderRunView(
      {},
      "/?panes=editor,browser",
      false,
      <SelectBlockOnCanvas label="login" />,
    );

    fireEvent.click(getByRole("button", { name: /canvas: select login/ }));

    const search = getByTestId("location-search").textContent ?? "";
    expect(search).not.toContain("active=");
    expect(search).not.toContain("wr=");
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

  test("a Copilot-focused watched run stays unselected when it finishes", () => {
    seedWatchedRun(Status.Running);
    const view = renderRunView({}, "/?wr=wr_1&wrs=copilot");

    seedWatchedRun(Status.Completed);
    view.rerenderRunView();

    expect(useRunViewStore.getState().pinnedFrameId).toBeNull();
    expect(view.getByTestId("location-search").textContent).not.toContain(
      "active=",
    );
  });

  test("an explicit timeline pin still wins during Copilot focus", () => {
    seedWatchedRun(Status.Running);
    const view = renderRunView({}, "/?wr=wr_1&wrs=copilot");

    act(() => useRunViewStore.getState().pinFrame("act_1"));

    expect(useRunViewStore.getState().pinnedFrameId).toBe("act_1");
    expect(view.getByTestId("location-search").textContent).toContain(
      "active=act_1",
    );

    seedWatchedRun(Status.Completed);
    view.rerenderRunView();
    expect(useRunViewStore.getState().pinnedFrameId).toBe("act_1");
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

describe("RunView failure presentation", () => {
  // A code block that killed the run, preceded in the (newest-first) timeline
  // by a block that still ran after it — so the last frame is NOT the failure.
  function seedFailedCodeRun(
    blockFailureReason = "CodeBlock failed with NameError at line 6: name 'min' is not defined.",
    errorCode = "user_code_error",
  ) {
    seedCompletedRun({
      status: Status.Failed,
      failure_reason: `code block failed. failure reason: ${blockFailureReason}`,
    });
    mocks.timeline = [
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_cleanup",
          label: "cleanup",
          status: Status.Completed,
          actions: [buildAction({ action_id: "act_cleanup", action_order: 0 })],
        }),
      ),
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_code",
          label: "scrape-prices",
          block_type: "code",
          status: Status.Failed,
          error_codes: [errorCode],
          failure_reason: blockFailureReason,
        }),
      ),
    ];
  }

  test("leads with the failure headline and working Fix and Retry CTAs", () => {
    seedCompletedRun({
      status: Status.Failed,
      failure_reason: "Login page rejected the credentials",
    });
    const onFix = vi.fn();
    const onRetry = vi.fn();
    const { container, getByTestId } = renderRunView({ onFix, onRetry });
    const line = within(getByTestId("run-failure-line"));

    expect(
      line.getByText("Login page rejected the credentials"),
    ).not.toBeNull();
    fireEvent.click(line.getByRole("button", { name: "Fix with Copilot" }));
    expect(onFix).toHaveBeenCalledTimes(1);
    fireEvent.click(line.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    // The card is gone: nothing on the page announces itself as an alert.
    expect(within(container).queryByRole("alert")).toBeNull();
  });

  test("Fix passes the failing block's label", () => {
    seedCompletedRun({
      status: Status.Failed,
      failure_reason: "Login page rejected the credentials",
    });
    mocks.timeline = [
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_checkout",
          label: "checkout",
          status: Status.Failed,
        }),
      ),
    ];
    const onFix = vi.fn();
    const { container } = renderRunView({ onFix, onRetry: vi.fn() });

    fireEvent.click(
      within(container).getByRole("button", { name: "Fix with Copilot" }),
    );

    expect(onFix.mock.calls[0]?.[0]).toBe("checkout");
  });

  test("offers no repair while the previous run's payload is still on screen", () => {
    seedCompletedRun({
      status: Status.Failed,
      failure_reason: "Login page rejected the credentials",
    });
    mocks.isPlaceholderData = true;

    const { container } = renderRunView({ onFix: vi.fn(), onRetry: vi.fn() });

    expect(
      within(container).queryByRole("button", { name: "Fix with Copilot" }),
    ).toBeNull();
  });

  test("keeps the technical detail one hover away from the headline", () => {
    seedCompletedRun({
      status: Status.Failed,
      failure_reason:
        "for_loop block failed. failure reason: Failed to execute code block. Reason: Exception: boom\\n\\tstack trace",
    });
    const { getByTestId } = renderRunView();
    const line = within(getByTestId("run-failure-line"));

    const headline = line.getByText("for_loop block failed");
    expect(headline.getAttribute("title")).toContain(
      "Exception: boom\n  stack trace",
    );
    expect(headline.getAttribute("title")).not.toContain("\\n");
    expect(headline.getAttribute("title")).not.toContain("\\t");
    expect(line.queryByText(/Exception: boom/)).toBeNull();
  });

  test("shows no failure line for a user-canceled run", () => {
    seedCompletedRun({
      status: Status.Canceled,
      failure_reason: "canceled by user",
    });
    const { container, queryByTestId } = renderRunView();
    const scope = within(container);

    expect(scope.queryByText("canceled by user")).toBeNull();
    expect(queryByTestId("run-failure-line")).toBeNull();
  });

  test("the block name jumps to the failing block, whose detail carries the expansion", () => {
    seedFailedCodeRun();
    const { container, getByTestId } = renderRunView({
      onFix: vi.fn(),
      onRetry: vi.fn(),
    });
    const line = within(getByTestId("run-failure-line"));

    expect(
      line.getByText("— The block's code raised NameError"),
    ).not.toBeNull();
    // One line at run level: badges and the disclosure live on the block.
    expect(line.queryByText("Line 6")).toBeNull();
    expect(line.queryByText("Technical details")).toBeNull();

    fireEvent.click(line.getByRole("button", { name: "scrape-prices" }));

    expect(useRunViewStore.getState().pinnedFrameId).toBe("wrb_code");
    const scope = within(container);
    expect(scope.getByText("Line 6")).not.toBeNull();
    expect(scope.getByText("Error code user_code_error")).not.toBeNull();
    expect(
      scope.getByText(/Open the block and fix the line that raised/),
    ).not.toBeNull();
    expect(scope.getByText("Technical details")).not.toBeNull();
  });

  test("the detail expands the line without restating it", () => {
    seedCompletedRun({
      status: Status.Failed,
      failure_reason:
        "code block failed. failure reason: CodeBlock failed with NameError at line 6: name 'min' is not defined.",
    });
    mocks.timeline = [
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_code",
          label: "scrape-prices",
          block_type: "code",
          status: Status.Failed,
          error_codes: ["user_code_error"],
          failure_reason:
            "CodeBlock failed with NameError at line 6: name 'min' is not defined.",
          actions: [
            buildAction({
              action_id: "act_fail",
              action_order: 1,
              status: Status.Failed,
            }),
            buildAction({ action_id: "act_ok", action_order: 0 }),
          ],
        }),
      ),
    ];
    const { container } = renderRunView(
      { onFix: vi.fn(), onRetry: vi.fn() },
      "/?wr=wr_1",
    );
    const scope = within(container);

    // Pinned on the failing block: the title lives on the line only; the
    // detail keeps guidance, badges and the disclosure.
    expect(useRunViewStore.getState().pinnedFrameId).toBe("wrb_code");
    expect(
      scope.getByText("— The block's code raised NameError"),
    ).not.toBeNull();
    expect(scope.queryByText("The block's code raised NameError")).toBeNull();
    expect(
      scope.getByText(/Open the block and fix the line that raised/),
    ).not.toBeNull();
    expect(scope.getByText("Line 6")).not.toBeNull();

    // Reached through one of its action rows, the block's failure still expands
    // (SKY-12240 keeps it on every action's Summary) and still skips the title.
    for (const actionId of ["act_ok", "act_fail"]) {
      act(() => useRunViewStore.getState().pinFrame(actionId));
      expect(scope.getByText("Failure")).not.toBeNull();
      expect(scope.getByText("Line 6")).not.toBeNull();
      expect(scope.queryByText("The block's code raised NameError")).toBeNull();
    }
  });

  test("a failed run's deep link lands on the failing block, not the last item", () => {
    seedFailedCodeRun();
    renderRunView({}, "/?wr=wr_1");
    expect(useRunViewStore.getState().pinnedFrameId).toBe("wrb_code");
  });

  test("a failed Copilot-focused run does not auto-pin its failing block", () => {
    seedFailedCodeRun();
    const { getByTestId } = renderRunView({}, "/?wr=wr_1&wrs=copilot");

    expect(useRunViewStore.getState().pinnedFrameId).toBeNull();
    expect(getByTestId("location-search").textContent).not.toContain("active=");
  });

  test("a Copilot-focused latest failed run does not auto-pin its failing block", () => {
    seedFailedCodeRun();
    const { getByTestId } = renderRunView({}, "/?wrs=copilot");

    expect(useRunViewStore.getState().pinnedFrameId).toBeNull();
    expect(getByTestId("location-search").textContent).not.toContain("active=");
  });

  test("a failed latest-run Studio open lands on the failing block", () => {
    seedFailedCodeRun();
    const { getByTestId } = renderRunView();

    expect(useRunViewStore.getState().pinnedFrameId).toBe("wrb_code");
    expect(getByTestId("location-search").textContent).toContain(
      "active=wrb_code",
    );
  });

  test("a finally-only failure keeps its strip label and jump", () => {
    seedCompletedRun({
      status: Status.Failed,
      failure_reason: "cleanup failed: Invalid master password",
      workflow: {
        workflow_definition: { blocks: [], finally_block_label: "cleanup" },
      },
    });
    mocks.timeline = [
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_cleanup",
          label: "cleanup",
          status: Status.Failed,
          failure_reason: "cleanup failed: Invalid master password",
        }),
      ),
    ];

    const { getByTestId } = renderRunView();
    const line = within(getByTestId("run-failure-line"));
    fireEvent.click(line.getByRole("button", { name: "cleanup" }));

    expect(useRunViewStore.getState().pinnedFrameId).toBe("wrb_cleanup");
  });

  test("the block detail's screenshot action opens the Browser pane and pins the block", async () => {
    getSpy.mockResolvedValue({
      data: [
        {
          artifact_id: "art_screenshot",
          artifact_type: ArtifactType.ActionScreenshot,
          created_at: "2026-08-27T00:00:00Z",
          modified_at: "2026-08-27T00:00:00Z",
          organization_id: "org_1",
          task_id: "task_1",
          step_id: "step_1",
          uri: "s3://bucket/screenshot.png",
        },
      ],
    });
    seedFailedCodeRun(
      "CodeBlock failed because a browser operation failed at line 4.",
      "browser_operation_failed",
    );
    const { container, getByTestId } = renderRunView({}, "/?wr=wr_1");
    expect(useRunViewStore.getState().pinnedFrameId).toBe("wrb_code");

    fireEvent.click(
      await within(container).findByRole("button", {
        name: "View block screenshot",
      }),
    );

    await waitFor(() =>
      expect(getByTestId("location-search").textContent).toContain("browser"),
    );
    await waitFor(() =>
      expect(useRunViewStore.getState().pinnedFrameId).toBe("wrb_code"),
    );
  });

  test("hides the screenshot link when the failed block has no capture", async () => {
    getSpy.mockResolvedValue({ data: [] });
    seedCompletedRun({
      status: Status.Failed,
      failure_reason:
        "code block failed. failure reason: CodeBlock failed because a browser operation failed at line 4.",
    });
    mocks.timeline = [
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_failed",
          block_type: "code",
          status: Status.Failed,
          error_codes: ["browser_operation_failed"],
          failure_reason:
            "CodeBlock failed because a browser operation failed at line 4.",
        }),
      ),
    ];

    const { container, getByTestId } = renderRunView(
      { onFix: vi.fn(), onRetry: vi.fn() },
      "/?wr=wr_1",
      false,
      <ArtifactsQueryProbe workflowRunBlockId="wrb_failed" />,
    );
    const line = within(getByTestId("run-failure-line"));
    const scope = within(container);
    // The link is also absent while the artifact query is in flight, so wait
    // for the empty result to reach the DOM, not just for the request to start.
    await waitFor(() =>
      expect(getByTestId("artifacts-query").textContent).toBe("success"),
    );

    // Absent from the a11y tree entirely, disabled included: a link into an
    // empty Browser pane is worse than no link.
    expect(
      scope.queryByRole("button", {
        name: "View block screenshot",
        hidden: true,
      }),
    ).toBeNull();
    expect(line.getByRole("button", { name: "Retry" })).not.toBeNull();
  });

  test("a sandbox fault offers a retry instead of a copilot fix", () => {
    seedFailedCodeRun(
      "Secure CodeBlock runner is unavailable. Please retry.",
      "runner_unavailable",
    );
    const onFix = vi.fn();
    const onRetry = vi.fn();
    const { getByTestId } = renderRunView({ onFix, onRetry });
    const line = within(getByTestId("run-failure-line"));

    expect(line.getByText("— The code sandbox was unreachable")).not.toBeNull();
    expect(line.queryByRole("button", { name: "Fix with Copilot" })).toBeNull();

    const retry = line.getByRole("button", { name: "Retry" });
    expect(retry.className).toContain("bg-cta");
    fireEvent.click(retry);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  test("a code block that continued on failure does not retitle the line", () => {
    seedCompletedRun({
      status: Status.Failed,
      failure_reason: "task block failed. failure reason: Login rejected",
    });
    mocks.timeline = [
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_code",
          block_type: "code",
          status: Status.Failed,
          continue_on_failure: true,
          error_codes: ["runner_unavailable"],
          failure_reason:
            "Secure CodeBlock runner is unavailable. Please retry.",
        }),
      ),
    ];
    const { getByTestId } = renderRunView();
    const line = within(getByTestId("run-failure-line"));

    expect(line.getByText("task block failed")).not.toBeNull();
    expect(line.queryByText(/code sandbox was unreachable/)).toBeNull();
  });

  test("hides the failure line outside the Timeline view", () => {
    seedCompletedRun({
      status: Status.Failed,
      failure_reason: "Login page rejected the credentials",
    });
    const { container, queryByTestId } = renderRunView();
    const scope = within(container);

    expect(
      scope.getByText("Login page rejected the credentials"),
    ).not.toBeNull();

    fireEvent.click(scope.getByRole("button", { name: "Outputs" }));
    expect(queryByTestId("run-failure-line")).toBeNull();
    expect(scope.queryByText("Login page rejected the credentials")).toBeNull();
  });

  test("an unlabeled failed block remains the deep-link and strip recovery target", () => {
    seedCompletedRun({
      status: Status.Failed,
      failure_reason: "Login page rejected the credentials",
    });
    mocks.timeline = [
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_unlabeled",
          label: null,
          status: Status.Failed,
          failure_reason: "Login page rejected the credentials",
        }),
      ),
    ];
    const { container, getByTestId } = renderRunView(
      { onFix: vi.fn(), onRetry: vi.fn() },
      "/?wr=wr_1",
    );
    const line = within(getByTestId("run-failure-line"));
    const scope = within(container);

    expect(useRunViewStore.getState().pinnedFrameId).toBe("wrb_unlabeled");
    expect(
      line.getByRole("button", { name: "Fix with Copilot" }),
    ).not.toBeNull();
    expect(line.getByRole("button", { name: "Retry" })).not.toBeNull();
    // The pinned block's own reason is identical to the run's, so it's fully
    // stated on the strip already: the block detail must not repeat it.
    expect(
      scope.getAllByText("Login page rejected the credentials"),
    ).toHaveLength(1);
    expect(scope.queryByText("Failure")).toBeNull();
  });

  test("a block reason that diverges from the run's own headline is never dropped", () => {
    seedCompletedRun({
      status: Status.Failed,
      failure_reason: "Task failed",
    });
    mocks.timeline = [
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_mismatch",
          label: "submit-form",
          status: Status.Failed,
          failure_reason: "POST returned 401. The token expired.",
        }),
      ),
    ];
    const { container } = renderRunView(
      { onFix: vi.fn(), onRetry: vi.fn() },
      "/?wr=wr_1",
    );
    const scope = within(container);

    expect(useRunViewStore.getState().pinnedFrameId).toBe("wrb_mismatch");
    // The strip states the run's own generic headline ("Task failed"); the
    // pinned block's more specific reason was never actually shown there,
    // so the detail panel must still say it in full.
    expect(
      scope.getByText("POST returned 401. The token expired."),
    ).not.toBeNull();
  });

  test("a non-culprit failed block is never suppressed just because its headline text matches", () => {
    seedCompletedRun({
      status: Status.Failed,
      failure_reason: "Network timeout",
    });
    mocks.timeline = [
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_early",
          label: "flaky-request",
          status: Status.Failed,
          continue_on_failure: true,
          failure_reason: "Network timeout",
        }),
      ),
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_last",
          label: "final-request",
          status: Status.Failed,
          failure_reason: "Network timeout",
        }),
      ),
    ];
    const { container } = renderRunView(
      { onFix: vi.fn(), onRetry: vi.fn() },
      "/?wr=wr_1",
    );

    // Auto-pin lands on the culprit — the strip's headline is about this
    // block, so its own identical reason is correctly suppressed.
    expect(useRunViewStore.getState().pinnedFrameId).toBe("wrb_last");
    expect(within(container).queryByText("Failure")).toBeNull();

    // Navigating to the earlier continue_on_failure block, whose parsed
    // headline happens to be the same text, must NOT suppress it: the
    // strip never actually described this block.
    act(() => useRunViewStore.getState().pinFrame("wrb_early"));
    const scope = within(container);
    expect(scope.getByText("Failure")).not.toBeNull();
    expect(scope.getByText("Network timeout")).not.toBeNull();
  });
});

describe("RunView live affordances", () => {
  test("an unavailable status quietly removes live claims while polling recovers", () => {
    seedRunningRun();
    mocks.statusUnavailable = true;
    const { container } = renderRunView();
    const scope = within(container);

    expect(scope.queryByText("Run status unavailable")).toBeNull();
    expect(scope.queryByText("Status unavailable")).toBeNull();
    expect(scope.queryByText("running", { exact: false })).toBeNull();
    expect(
      scope.queryByRole("button", { name: "Watch live in the Browser pane" }),
    ).toBeNull();

    expect(scope.queryByRole("button", { name: "Retry status" })).toBeNull();
    expect(mocks.refetchRunStatus).not.toHaveBeenCalled();
  });

  test("a running run shows the Live chip; clicking hands off to the Browser pane", () => {
    seedRunningRun();
    useRunViewStore.getState().pinFrame("act_1");
    // Park the Browser pane on a pinned replay view: the Live CTA promises
    // live, so the pinned pill must not swallow the handoff.
    useStudioBrowserStore.setState({ view: "screenshots" });
    const { container, getByTestId } = renderRunView();
    const scope = within(container);

    fireEvent.click(
      scope.getByRole("button", { name: "Watch live in the Browser pane" }),
    );

    // Unpins to the live edge and pins the Browser pane's view intent to live.
    expect(useRunViewStore.getState().pinnedFrameId).toBeNull();
    expect(useStudioBrowserStore.getState().view).toBe("live");
    expect(getByTestId("location-search").textContent).toContain("browser");
  });

  test("a queued run shows only the queued status pill — no Live chip, no banner", () => {
    seedCompletedRun({ status: Status.Queued });
    const { container } = renderRunView();
    const scope = within(container);

    expect(scope.getAllByText("queued", { exact: false }).length).toBe(1);
    expect(scope.queryByText(/Run queued/)).toBeNull();
    expect(
      scope.queryByRole("button", { name: "Watch live in the Browser pane" }),
    ).toBeNull();
  });

  test("a queued run reports no elapsed time in the strip or the timeline", () => {
    // created_at is always populated by the API, so only started_at can decide
    // whether the run has actually accrued elapsed time.
    seedCompletedRun({
      status: Status.Queued,
      created_at: "2026-06-30T23:59:00Z",
      queued_at: "2026-06-30T23:59:30Z",
      started_at: null,
      finished_at: null,
    });
    const { container } = renderRunView();
    const scope = within(container);

    expect(scope.queryByText(/^Ran for /)).toBeNull();
    expect(scope.queryByText(/·\s*\d+[ms]/)).toBeNull();
    expect(scope.queryByText(/·\s*—/)).toBeNull();
  });
});

describe("RunView iteration selection", () => {
  test("selecting the loop block after an iteration clears the iteration scope", () => {
    seedForLoopRun();
    // Seed ?active= so the loop is the selected (and expanded) item on mount,
    // making its iteration rows visible.
    const { container } = renderRunView({}, "/?active=wrb_loop");
    const scope = within(container);

    // The detail header's meta line is the one place the selected iteration
    // and its value render. Baseline: the loop's own current iteration.
    const headerMeta = () =>
      container.querySelector('[data-slot="block-detail-header-meta"]')
        ?.textContent ?? "";
    expect(headerMeta()).toContain("Iteration 1");
    expect(scope.getByText("alpha")).not.toBeNull();

    // Drill into iteration 2 (the timeline row).
    fireEvent.click(scope.getByText("Iteration 2"));
    expect(headerMeta()).toContain("Iteration 2");
    expect(scope.getByText("beta")).not.toBeNull();
    expect(scope.queryByText("alpha")).toBeNull();
    // The iteration scope is shared with the Browser pane via the store.
    expect(useRunViewStore.getState().activeIteration).toBe(1);

    // Click the loop block row (descriptor text is timeline-only). The detail
    // must fall back to the loop's own iteration instead of staying on 2.
    fireEvent.click(scope.getByText(/Loop over 2 values/));
    expect(headerMeta()).toContain("Iteration 1");
    expect(scope.getByText("alpha")).not.toBeNull();
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

  test("clicking an action row jumps the editor to the action's block and keeps the action pinned", () => {
    mocks.timeline = [
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_jump",
          label: "jump-target-block",
          actions: [buildAction({ action_id: "act_jump" })],
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
    const focusBlock = registerHandle();

    // ?active= on the block expands it so its action rows render.
    const { container } = renderRunView(
      {},
      "/?wr=wr_1&active=wrb_jump&panes=editor,overview",
    );
    // The row's name is its index glued to the sr-only action type.
    fireEvent.click(
      within(container).getByRole("button", { name: /^#1\s*Click$/ }),
    );

    expect(focusBlock).toHaveBeenCalledWith("node-jump");
    expect(useRunViewStore.getState().pinnedFrameId).toBe("act_jump");
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

    fireEvent.click(scope.getByRole("button", { name: "Outputs" }));

    expect(scope.getByText("Errors")).not.toBeNull();
    // A field, not an alert: no title prose, and each code renders once.
    expect(scope.queryByText("Run errors")).toBeNull();
    expect(scope.getAllByText("E_INVOICE_MISSING")).toHaveLength(1);
    expect(scope.getByText("E_PAYMENT_BLOCKED")).not.toBeNull();
    expect(
      scope.getByText("The expected invoice was not available."),
    ).not.toBeNull();
    expect(scope.queryByText("confidence_float")).toBeNull();
    expect(scope.getByText("Downloaded files")).not.toBeNull();
    expect(scope.getByText("report.pdf")).not.toBeNull();
  });

  test("surfaces the full run outputs below extracted information", () => {
    seedCompletedRun({
      outputs: {
        extracted_information: { answer: 42 },
        additional_output: "full-run-only",
      },
    });

    const { container } = renderRunView();
    const scope = within(container);

    fireEvent.click(scope.getByRole("button", { name: "Outputs" }));

    expect(scope.getByText("Extracted information")).not.toBeNull();
    expect(scope.getByText("Run outputs")).not.toBeNull();
  });

  test("shows a code block's returned outputs when there is no extracted information", () => {
    seedCompletedRun({
      outputs: {
        get_stars_output: { star_count: 22600, evidence_text: "22.6k stars" },
        extracted_information: [],
      },
    });

    const { container } = renderRunView();
    const scope = within(container);

    fireEvent.click(scope.getByRole("button", { name: "Outputs" }));

    expect(scope.queryByText("No outputs for this run")).toBeNull();
    expect(scope.getByText("Run outputs")).not.toBeNull();
    expect(scope.getAllByText("get_stars_output").length).toBeGreaterThan(0);
    expect(scope.getByText("22600")).not.toBeNull();
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
    expect(scope.queryByText("Errors")).toBeNull();
  });

  test("renders a genuinely omitted output as an empty Outputs state", () => {
    seedCompletedRun(sealedSavedRunReplayOverrides());

    const { container } = renderRunView();
    const scope = within(container);

    fireEvent.click(scope.getByRole("button", { name: "Outputs" }));
    expect(scope.getByText("No outputs for this run")).not.toBeNull();
    expect(scope.queryByText("Errors")).toBeNull();
    expect(scope.queryByText("Downloaded files")).toBeNull();
  });

  test("does not substitute persisted Copilot prose for a genuinely omitted output", async () => {
    const corroboratingProse =
      "Copilot found 22.9k stars, matching the value visible in the source.";
    getSpy.mockImplementation((path: string) => {
      if (path === "/workflow/copilot/chat-history") {
        return Promise.resolve({
          data: {
            workflow_copilot_chat_id: "chat_1",
            chat_history: [
              {
                sender: "ai",
                content: corroboratingProse,
                created_at: "2026-08-29T11:01:20Z",
              },
            ],
            proposed_workflow: null,
            auto_accept: false,
          },
        });
      }
      return Promise.resolve({ data: [] });
    });
    seedCompletedRun(sealedSavedRunReplayOverrides());
    const copilotPortal = document.createElement("div");
    document.body.appendChild(copilotPortal);

    const { container } = renderRunView(
      {},
      "/",
      false,
      <WorkflowPermanentIdContext.Provider value="wpid_1">
        <WorkflowCopilotChat docked portalTarget={copilotPortal} />
      </WorkflowPermanentIdContext.Provider>,
    );
    const runView = within(container);

    await waitFor(() =>
      expect(screen.getByText(corroboratingProse)).not.toBeNull(),
    );
    fireEvent.click(runView.getByRole("button", { name: "Outputs" }));

    expect(runView.getByText("No outputs for this run")).not.toBeNull();
    expect(runView.queryByText(corroboratingProse)).toBeNull();
    expect(runView.queryByText("22.9k")).toBeNull();

    copilotPortal.remove();
  });
});

describe("a run whose payload is still withheld", () => {
  test("shows the loading placeholder instead of the empty-run CTA", () => {
    mocks.isPlaceholderData = true;
    const { container } = renderRunView();

    expect(container.textContent).toContain("Workflow run is loading\u2026");
    expect(container.textContent).not.toContain(
      "Run the workflow to watch it live here.",
    );
  });

  test("still states the empty-run CTA when no run is being loaded", () => {
    const { container } = renderRunView();

    expect(container.textContent).toContain(
      "Run the workflow to watch it live here.",
    );
  });

  test("does not paint a retained timeline from the previous run", () => {
    // The timeline payload carries no run id, so the identity select cannot reach
    // it — a run switch serves the prior run's rows alongside the new run.
    seedCompletedRun();
    mocks.timeline = [
      buildBlockItem(buildBlock({ label: "PreviousRunBlock" })),
    ];
    mocks.isPlaceholderData = true;
    const { container } = renderRunView();

    expect(container.textContent).not.toContain("PreviousRunBlock");
  });

  test("states the empty-run CTA once the requested run id is cleared", () => {
    // A disabled query never fetches again, so isPlaceholderData stays true for
    // as long as the pane is mounted. Without the run-id term the placeholder
    // would hold the pane on "loading" permanently.
    mocks.isPlaceholderData = true;
    const { container } = renderRunView({ workflowRunId: undefined });

    expect(container.textContent).toContain(
      "Run the workflow to watch it live here.",
    );
    expect(container.textContent).not.toContain("Workflow run is loading…");
  });
});
