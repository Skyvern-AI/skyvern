import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  WorkflowCopilotChat,
  canonicalRecoveriesByWorkflow,
} from "./WorkflowCopilotChat";

import { FeatureFlagContext } from "@/hooks/useFeatureFlag";
import { useStudioBrowserStore } from "@/store/useStudioBrowserStore";

type StreamBody = {
  message: string;
  workflow_run_id?: string | null;
};
type StreamCall = {
  body: StreamBody;
  onMessage: (payload: unknown) => boolean;
  resolve: () => void;
  reject: (error: unknown) => void;
};

const {
  streamCalls,
  postStreaming,
  cancelPost,
  historyResponse,
  routeParams,
  timelineGet,
} = vi.hoisted(() => {
  const calls: StreamCall[] = [];
  const post = vi.fn().mockResolvedValue({});
  const streaming = vi.fn(
    (
      _path: string,
      body: StreamBody,
      onMessage: (payload: unknown) => boolean,
    ) =>
      new Promise<void>((resolve, reject) => {
        calls.push({ body, onMessage, resolve, reject });
      }),
  );
  const history = {
    data: {
      workflow_copilot_chat_id: null as string | null,
      chat_history: [] as unknown[],
      proposed_workflow: null as Record<string, unknown> | null,
      auto_accept: false,
    },
  };
  const params = {
    current: {
      workflowPermanentId: "wpid_1",
      workflowRunId: undefined as string | undefined,
    },
  };
  const timeline = vi.fn().mockResolvedValue({ data: [] });
  return {
    streamCalls: calls,
    postStreaming: streaming,
    cancelPost: post,
    historyResponse: history,
    routeParams: params,
    timelineGet: timeline,
  };
});

vi.mock("@/api/sse", () => ({
  getSseClient: vi.fn().mockResolvedValue({ postStreaming }),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn().mockResolvedValue({
    get: vi.fn().mockImplementation((url: string) => {
      if (url.includes("/timeline")) return timelineGet(url);
      return Promise.resolve(historyResponse);
    }),
    post: cancelPost,
  }),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

const { switchStudioRun, navigate } = vi.hoisted(() => ({
  switchStudioRun: vi.fn(),
  navigate: vi.fn(),
}));

vi.mock(
  "@/routes/workflows/studio/runSwitchNavigation",
  async (importOriginal) => ({
    ...(await importOriginal<
      typeof import("@/routes/workflows/studio/runSwitchNavigation")
    >()),
    useSwitchStudioRun: () => switchStudioRun,
  }),
);

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return {
    ...actual,
    useParams: () => routeParams.current,
    useSearchParams: () => [new URLSearchParams(), vi.fn()],
    useNavigate: () => navigate,
    useLocation: () => ({
      pathname: "/",
      search: "",
      hash: "",
      state: null,
      key: "default",
    }),
  };
});

// The real hook needs a QueryClientProvider this harness doesn't set up.
const { focusedRun, reopenEditor } = vi.hoisted(() => ({
  focusedRun: { current: undefined as Record<string, unknown> | undefined },
  reopenEditor: vi.fn(),
}));
vi.mock("../hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQueries: (ids: readonly string[]) =>
    ids.map(() => ({ data: undefined })),
  useWorkflowRunQuery: ({ workflowRunId }: { workflowRunId?: string }) => ({
    data: workflowRunId ? focusedRun.current : undefined,
  }),
  useWorkflowRunByIdQuery: () => ({ data: undefined, isError: false }),
}));

vi.mock(
  "@/routes/workflows/studio/StudioPaneDefaultsContext",
  async (importOriginal) => {
    const actual =
      await importOriginal<
        typeof import("@/routes/workflows/studio/StudioPaneDefaultsContext")
      >();
    return {
      ...actual,
      useStudioPaneDefaults: () => ({
        ...actual.useStudioPaneDefaults(),
        reopenEditor,
      }),
    };
  },
);

const saveData = {
  title: "Test WF",
  workflow: {
    workflow_id: "wf_1",
    workflow_permanent_id: "wpid_1",
    description: "",
    totp_verification_url: null,
    is_saved_task: false,
    status: "published",
  },
  settings: {
    proxyLocation: null,
    webhookCallbackUrl: null,
    persistBrowserSession: false,
    browserProfileId: null,
    browserProfileKey: null,
    model: null,
    maxScreenshotScrolls: null,
    extraHttpHeaders: null,
    runWith: "agent",
    scriptCacheKey: "",
    aiFallback: true,
    codeVersion: 2,
    runSequentially: false,
    sequentialKey: null,
  },
  parameters: [],
  blocks: [],
  workflowDefinitionVersion: 1,
};

vi.mock("@/store/WorkflowHasChangesStore", () => {
  const state = { getSaveData: () => saveData, setSaveBlockedReason: () => {} };
  return {
    useWorkflowHasChangesStore: Object.assign(() => state, {
      getState: () => state,
    }),
  };
});

import { useWorkflowBlockSearchStore } from "@/store/WorkflowBlockSearchStore";

const BOOLEAN_FLAGS: Record<string, boolean> = {
  WORKFLOW_COPILOT_CODE_BLOCK_MODE: false,
  CODE_BLOCK_ACCESS: false,
};

type ChatProps = {
  docked?: boolean;
  portalTarget?: HTMLElement | null;
  workflowRunId?: string | null;
};

function chatUi(props: ChatProps = {}) {
  return (
    <FeatureFlagContext.Provider value={(name) => BOOLEAN_FLAGS[name]}>
      <WorkflowCopilotChat {...props} />
    </FeatureFlagContext.Provider>
  );
}

// A docked chat portals its content, rendering null without a body-attached target.
function makeDockedProps(): ChatProps {
  const portalTarget = document.createElement("div");
  document.body.appendChild(portalTarget);
  return { docked: true, portalTarget };
}

async function renderChat(props: ChatProps = {}) {
  const view = render(chatUi(props));
  await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
  return view;
}

async function submit(value: string) {
  fireEvent.change(screen.getByRole("textbox"), { target: { value } });
  fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
  await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
}

const runOutcomeFrame = (overrides: Partial<Record<string, unknown>> = {}) => ({
  type: "run_outcome",
  workflow_run_id: "wr_1",
  workflow_run_block_ids: ["wrb_1"],
  block_labels: ["block_1"],
  verdict: "evaluating",
  iteration: 0,
  timestamp: "2026-06-10T00:00:00Z",
  ...overrides,
});

const blockProgressFrame = (
  overrides: Partial<Record<string, unknown>> = {},
) => ({
  type: "block_progress",
  workflow_run_block_id: "wrb_1",
  block_label: "block_1",
  block_type: "code",
  status: "running",
  iteration: 0,
  timestamp: "2026-06-10T00:00:00Z",
  ...overrides,
});

const runStartedFrame = (overrides: Partial<Record<string, unknown>> = {}) => ({
  type: "run_started",
  workflow_run_id: "wr_1",
  timestamp: "2026-06-10T00:00:00Z",
  ...overrides,
});

const agentStepCall = (index: number, status: string, goal: string) => ({
  index,
  tool: "page_extract",
  status,
  started_at: `2026-06-10T00:00:0${index + 1}Z`,
  finished_at: status === "running" ? null : `2026-06-10T00:00:0${index + 2}Z`,
  arguments: JSON.stringify({ prompt: goal }),
});

const agentStepTimeline = (
  steps: Array<{ id: string; label: string; status: string; calls: unknown[] }>,
) => ({
  data: steps.map((step) => ({
    type: "block",
    block: {
      workflow_run_block_id: step.id,
      block_type: "agent_step",
      label: step.label,
      status: step.status,
      actions: [],
      tool_calls: step.calls,
    },
    children: [],
    thought: null,
    created_at: "2026-06-10T00:00:00Z",
    modified_at: "2026-06-10T00:00:00Z",
  })),
});

const agentStepFrame = (id: string, label: string, status: string) =>
  blockProgressFrame({
    workflow_run_id: "wr_1",
    workflow_run_block_id: id,
    block_label: label,
    block_type: "agent_step",
    status,
  });

function seedReloadedAgentStepTurn() {
  const activity = (kind: "tool_call" | "tool_result", second: number) => ({
    kind,
    text: "Testing workflow",
    iteration: 0,
    toolName: "run_blocks_and_collect_debug",
    displayLabel: "Testing workflow",
    id: `${kind === "tool_call" ? "tc" : "tr"}-call_test_end_to_end_turn-1`,
    timestamp: `2026-06-10T00:00:${second}Z`,
    activityStartedAt: "2026-06-10T00:00:00Z",
    activityBucket: { kind: "design" },
    ...(kind === "tool_result" ? { success: true, workflowRunId: "wr_1" } : {}),
  });
  const savedBlock = (
    id: string,
    label: string,
    blockType: string,
    extra: Record<string, unknown> = {},
  ) => ({
    label,
    workflowRunBlockId: id,
    workflowRunId: "wr_1",
    blockType,
    state: "completed",
    lastSeenIteration: 0,
    activity: [],
    startedAt: "2026-06-10T00:00:01Z",
    endedAt: "2026-06-10T00:00:20Z",
    ...extra,
  });
  historyResponse.data = {
    workflow_copilot_chat_id: "chat_1",
    chat_history: [
      {
        sender: "user",
        content: "test it",
        created_at: "2026-06-10T00:00:00Z",
      },
      {
        sender: "ai",
        content: "Tested the workflow.",
        created_at: "2026-06-10T00:00:30Z",
        narrative_payload: {
          turnId: "turn-1",
          turnIndex: 0,
          designStarted: true,
          designEnded: true,
          draft: null,
          terminal: "response",
          terminalMessage: "Tested the workflow.",
          narrativeSummary: "Tested the workflow.",
          designActivity: [
            activity("tool_call", 0),
            activity("tool_result", 25),
          ],
          blocks: [
            savedBlock("wrb_step", "research", "agent_step"),
            savedBlock("wrb_child", "research_task_1", "task_v2", {
              parentWorkflowRunBlockId: "wrb_step",
            }),
          ],
        },
      },
    ],
    proposed_workflow: null,
    auto_accept: false,
  };
}

// The step finished one call; its child recorded an action the reloaded child card must not gain.
function reloadedAgentStepTimeline() {
  const timeline = agentStepTimeline([
    {
      id: "wrb_step",
      label: "research",
      status: "completed",
      calls: [agentStepCall(0, "completed", "Step call goal")],
    },
  ]);
  timeline.data[0]!.children = [
    {
      type: "block",
      block: {
        workflow_run_block_id: "wrb_child",
        block_type: "task_v2",
        label: "research_task_1",
        status: "completed",
        actions: [
          {
            action_id: "child_a1",
            action_type: "wobble_gizmo",
            status: "completed",
            task_id: null,
            step_id: null,
            step_order: null,
            action_order: 0,
            confidence_float: null,
            description: null,
            reasoning: "Child action reasoning",
            intention: null,
            response: null,
            created_by: null,
            text: null,
            output: { duration_ms: 120 },
          },
        ],
      },
      children: [],
      thought: null,
      created_at: "2026-06-10T00:00:00Z",
      modified_at: "2026-06-10T00:00:00Z",
    },
  ] as never[];
  return timeline;
}

async function openReloadedStepCard() {
  await waitFor(() =>
    expect(screen.getByText("Tested the workflow.")).toBeTruthy(),
  );
  const fold = screen.queryByRole("button", { name: /^Worked through/ });
  if (fold !== null && fold.getAttribute("aria-expanded") !== "true") {
    fireEvent.click(fold);
  }
  document
    .querySelectorAll<HTMLElement>('[data-activity-line="true"]')
    .forEach((line) => {
      if (line.getAttribute("aria-expanded") === "false") {
        fireEvent.click(line);
      }
    });
  const step = screen.getAllByTestId("copilot-test-card")[0]!;
  fireEvent.click(within(step).getAllByRole("button")[0]!);
}

async function startDesignTurn() {
  await renderChat();
  await submit("build a workflow");
  streamCalls[0]!.onMessage({
    type: "turn_start",
    turn_id: "turn-1",
    turn_index: 0,
    mode: "build",
    timestamp: "2026-06-10T00:00:00Z",
  });
  streamCalls[0]!.onMessage({
    type: "design_start",
    timestamp: "2026-06-10T00:00:00Z",
  });
}

async function endTurnWithRunLive() {
  streamCalls[0]!.onMessage({
    type: "response",
    workflow_copilot_chat_id: "chat_1",
    message: "Ran out of time.",
    response_time: "2026-06-10T00:00:06Z",
    proposal_disposition: "no_proposal",
    turn_id: "turn-1",
    narrative_payload: null,
  });
  await act(async () => streamCalls[0]!.resolve());
  await waitFor(() =>
    expect(screen.getAllByText("Ran out of time.").length).toBeGreaterThan(0),
  );
}

beforeEach(() => {
  switchStudioRun.mockClear();
  navigate.mockClear();
  reopenEditor.mockClear();
  focusedRun.current = undefined;
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  streamCalls.length = 0;
  postStreaming.mockClear();
  cancelPost.mockClear();
  timelineGet.mockClear();
  timelineGet.mockResolvedValue({ data: [] });
  historyResponse.data = {
    workflow_copilot_chat_id: null,
    chat_history: [],
    proposed_workflow: null,
    auto_accept: false,
  };
  routeParams.current = {
    workflowPermanentId: "wpid_1",
    workflowRunId: undefined,
  };
});

afterEach(() => {
  cleanup();
  canonicalRecoveriesByWorkflow.clear();
});

describe("WorkflowCopilotChat — recorded-action live poll wiring", () => {
  it("starts a live poll from the first block_progress that carries a run id", async () => {
    await renderChat();
    await submit("build a workflow");

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "" } });
    streamCalls[0]!.onMessage(
      blockProgressFrame({ workflow_run_id: "wr_1", status: "running" }),
    );

    await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));
    expect(timelineGet.mock.calls[0]![0]).toBe(
      "/workflows/wpid_1/runs/wr_1/timeline",
    );
  });

  it("polls the timeline repeatedly while the run stays live", async () => {
    // Call through so React/testing-library timers keep working; we only need
    // to capture the registered callback to drive it deterministically.
    const setIntervalSpy = vi.spyOn(window, "setInterval");
    try {
      await renderChat();
      await submit("build a workflow");

      streamCalls[0]!.onMessage(
        blockProgressFrame({ workflow_run_id: "wr_1" }),
      );
      // Immediate fetch on the first sighting.
      await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));
      expect(setIntervalSpy).toHaveBeenCalledWith(expect.any(Function), 2500);

      // Drive the registered interval callback: each tick re-fetches (the
      // reducer merges by actionId, so repeated fetches never duplicate rows).
      const tick = setIntervalSpy.mock.calls.find((c) => c[1] === 2500)![0] as (
        ...args: unknown[]
      ) => void;
      tick();
      await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(2));
      tick();
      await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(3));
    } finally {
      setIntervalSpy.mockRestore();
    }
  });

  it("converges with a final fetch and stops polling on a terminal run_outcome", async () => {
    const clearIntervalSpy = vi.spyOn(window, "clearInterval");
    const setIntervalSpy = vi.spyOn(window, "setInterval");
    try {
      await renderChat();
      await submit("build a workflow");

      streamCalls[0]!.onMessage(
        blockProgressFrame({ workflow_run_id: "wr_1" }),
      );
      await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));
      const idx = setIntervalSpy.mock.calls.findIndex((c) => c[1] === 2500);
      const intervalId = setIntervalSpy.mock.results[idx]!.value;

      // Terminal verdict: one convergent fetch, then the interval is cleared.
      streamCalls[0]!.onMessage(runOutcomeFrame({ verdict: "demonstrated" }));
      await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(2));
      expect(clearIntervalSpy).toHaveBeenCalledWith(intervalId);
    } finally {
      setIntervalSpy.mockRestore();
      clearIntervalSpy.mockRestore();
    }
  });

  it("stops its own poll when the turn ends with the run still live", async () => {
    const clearIntervalSpy = vi.spyOn(window, "clearInterval");
    const setIntervalSpy = vi.spyOn(window, "setInterval");
    try {
      await renderChat();
      await submit("build a workflow");

      streamCalls[0]!.onMessage(
        blockProgressFrame({ workflow_run_id: "wr_1" }),
      );
      await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));
      const idx = setIntervalSpy.mock.calls.findIndex((c) => c[1] === 2500);
      const intervalId = setIntervalSpy.mock.results[idx]!.value;
      expect(clearIntervalSpy).not.toHaveBeenCalledWith(intervalId);

      streamCalls[0]!.onMessage({
        type: "response",
        workflow_copilot_chat_id: "chat_1",
        message: "Ran out of time.",
        response_time: "2026-06-10T00:00:06Z",
        proposal_disposition: "no_proposal",
        turn_id: "turn-1",
        narrative_payload: null,
      });
      await act(async () => streamCalls[0]!.resolve());

      await waitFor(() =>
        expect(clearIntervalSpy).toHaveBeenCalledWith(intervalId),
      );
      expect(timelineGet).toHaveBeenCalledTimes(1);
    } finally {
      setIntervalSpy.mockRestore();
      clearIntervalSpy.mockRestore();
    }
  });

  it("falls back to run_outcome(evaluating) when block_progress carries no run id", async () => {
    await renderChat();
    await submit("build a workflow");

    // Old backend: block_progress has no run id, so no poll can start yet.
    streamCalls[0]!.onMessage(blockProgressFrame({ status: "running" }));
    streamCalls[0]!.onMessage(blockProgressFrame({ status: "completed" }));
    expect(timelineGet).not.toHaveBeenCalled();

    // The run id first arrives on run_outcome — the poll starts there.
    streamCalls[0]!.onMessage(runOutcomeFrame({ verdict: "evaluating" }));
    await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));
    expect(timelineGet.mock.calls[0]![0]).toBe(
      "/workflows/wpid_1/runs/wr_1/timeline",
    );
  });

  it("never fetches when frames carry an empty workflow_run_id", async () => {
    await renderChat();
    await submit("build a workflow");

    streamCalls[0]!.onMessage(blockProgressFrame({ workflow_run_id: "" }));
    streamCalls[0]!.onMessage(runOutcomeFrame({ workflow_run_id: "" }));

    expect(timelineGet).not.toHaveBeenCalled();
  });

  it("renders the recorded actions once the timeline fetch resolves", async () => {
    timelineGet.mockResolvedValue({
      data: [
        {
          type: "block",
          block: {
            workflow_run_block_id: "wrb_1",
            actions: [
              {
                action_id: "a1",
                action_type: "wobble_gizmo",
                status: "completed",
                task_id: null,
                step_id: null,
                step_order: null,
                action_order: 0,
                confidence_float: null,
                description: null,
                reasoning: "Wobbled the gizmo into place",
                intention: null,
                response: null,
                created_by: null,
                text: null,
                output: { duration_ms: 220 },
              },
            ],
          },
          children: [],
          thought: null,
          created_at: "2026-06-10T00:00:00Z",
          modified_at: "2026-06-10T00:00:00Z",
        },
      ],
    });

    await renderChat();
    await submit("build a workflow");

    streamCalls[0]!.onMessage({
      type: "turn_start",
      turn_id: "turn-1",
      turn_index: 0,
      mode: "build",
      timestamp: "2026-06-10T00:00:00Z",
    });
    streamCalls[0]!.onMessage({
      type: "block_progress",
      workflow_run_block_id: "wrb_1",
      block_label: "block_1",
      block_type: "code",
      status: "running",
      iteration: 0,
      timestamp: "2026-06-10T00:00:00Z",
    });
    streamCalls[0]!.onMessage({
      type: "block_progress",
      workflow_run_block_id: "wrb_1",
      block_label: "block_1",
      block_type: "code",
      status: "completed",
      iteration: 0,
      timestamp: "2026-06-10T00:00:05Z",
    });
    streamCalls[0]!.onMessage(runOutcomeFrame());

    await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText("Wobble Gizmo")).toBeTruthy());
  });

  it("names a code block's raised error by its line and keeps its full text one click deep", async () => {
    const failure =
      "Locator.fill: Timeout 30000ms exceeded.\nCall log:\n  - waiting for the email field";
    timelineGet.mockResolvedValue({
      data: [
        {
          type: "block",
          block: {
            workflow_run_block_id: "wrb_1",
            actions: [
              {
                action_id: "a2",
                action_type: "null_action",
                status: "failed",
                task_id: null,
                step_id: null,
                step_order: null,
                action_order: 1,
                confidence_float: null,
                description: "code error at line 8",
                reasoning: null,
                intention: null,
                response: failure,
                created_by: null,
                text: null,
                output: { code_line: 8 },
              },
            ],
          },
          children: [],
          thought: null,
          created_at: "2026-06-10T00:00:00Z",
          modified_at: "2026-06-10T00:00:00Z",
        },
      ],
    });

    await renderChat();
    await submit("build a workflow");
    streamCalls[0]!.onMessage({
      type: "turn_start",
      turn_id: "turn-1",
      turn_index: 0,
      mode: "build",
      timestamp: "2026-06-10T00:00:00Z",
    });
    streamCalls[0]!.onMessage({
      type: "design_start",
      timestamp: "2026-06-10T00:00:00Z",
    });
    for (const [status, timestamp] of [
      ["running", "2026-06-10T00:00:00Z"],
      ["failed", "2026-06-10T00:00:31Z"],
    ]) {
      streamCalls[0]!.onMessage({
        type: "block_progress",
        workflow_run_id: "wr_1",
        workflow_run_block_id: "wrb_1",
        block_label: "block_1",
        block_type: "code",
        status,
        iteration: 0,
        timestamp,
      });
    }

    // The recorded step replays before it settles into its failure.
    await waitFor(() => expect(screen.getByText("line 8")).toBeTruthy(), {
      timeout: 3000,
    });
    expect(screen.getByText("Code error")).toBeTruthy();
    expect(screen.queryByText("Screenshot")).toBeNull();
    expect(
      screen.getByText("Locator.fill: Timeout 30000ms exceeded."),
    ).toBeTruthy();
    expect(screen.queryByText(/waiting for the email field/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show details" }));
    expect(screen.getByText(/waiting for the email field/)).toBeTruthy();
  });

  it("patches an already-frozen AI message when the timeline fetch resolves after the terminal response", async () => {
    let resolveTimeline!: (value: { data: unknown[] }) => void;
    timelineGet.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveTimeline = resolve;
        }),
    );

    await renderChat();
    await submit("build a workflow");

    streamCalls[0]!.onMessage({
      type: "turn_start",
      turn_id: "turn-1",
      turn_index: 0,
      mode: "build",
      timestamp: "2026-06-10T00:00:00Z",
    });
    streamCalls[0]!.onMessage({
      type: "block_progress",
      workflow_run_block_id: "wrb_1",
      block_label: "block_1",
      block_type: "code",
      status: "running",
      iteration: 0,
      timestamp: "2026-06-10T00:00:00Z",
    });
    streamCalls[0]!.onMessage({
      type: "block_progress",
      workflow_run_block_id: "wrb_1",
      block_label: "block_1",
      block_type: "code",
      status: "completed",
      iteration: 0,
      timestamp: "2026-06-10T00:00:05Z",
    });
    streamCalls[0]!.onMessage(runOutcomeFrame());
    await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));

    // Terminal response freezes the narrative BEFORE the timeline fetch
    // (started above) resolves.
    streamCalls[0]!.onMessage({
      type: "response",
      workflow_copilot_chat_id: "chat_1",
      message: "Done",
      response_time: "2026-06-10T00:00:06Z",
      proposal_disposition: "no_proposal",
      turn_id: "turn-1",
      narrative_payload: null,
    });
    // The bottom live bubble and the newly-frozen message both briefly carry
    // role="status"; wait for the live one to unmount (terminal narrative)
    // rather than grabbing whichever settles first.
    await waitFor(() => {
      expect(screen.getAllByRole("status")).toHaveLength(1);
    });

    resolveTimeline({
      data: [
        {
          type: "block",
          block: {
            workflow_run_block_id: "wrb_1",
            actions: [
              {
                action_id: "a1",
                action_type: "wobble_gizmo",
                status: "completed",
                task_id: null,
                step_id: null,
                step_order: null,
                action_order: 0,
                confidence_float: null,
                description: null,
                reasoning: "Wobbled the gizmo into place",
                intention: null,
                response: null,
                created_by: null,
                text: null,
                output: { duration_ms: 220 },
              },
            ],
          },
          children: [],
          thought: null,
          created_at: "2026-06-10T00:00:00Z",
          modified_at: "2026-06-10T00:00:00Z",
        },
      ],
    });

    // The current settled turn remains expanded, so expand the block row to
    // reach the replay the fetch just patched in — this is the reviewer's
    // "does the verify card ever receive it".
    const statusRegion = await waitFor(() => screen.getByRole("status"));
    // The row's primary text is the humanized label ("block_1" -> "Block 1"),
    // not the raw block label.
    fireEvent.click(within(statusRegion).getByText("Block 1"));

    await waitFor(() => expect(screen.getByText("Wobble Gizmo")).toBeTruthy());
  });

  it("drops an older timeline response that resolves after a newer one, so the step keeps its nested child", async () => {
    const pending: Array<(value: { data: unknown[] }) => void> = [];
    timelineGet.mockImplementation(
      () => new Promise((resolve) => pending.push(resolve)),
    );
    const timeline = (childId: string | null) => ({
      data: [
        {
          type: "block",
          block: {
            workflow_run_block_id: "wrb_step",
            block_type: "agent_step",
            label: "research",
            status: "running",
            actions: [],
            tool_calls: [
              {
                index: 0,
                tool: "browser_task",
                status: "running",
                started_at: "2026-06-10T00:00:01Z",
                finished_at: null,
                arguments: '{"navigation_goal": "Click Start"}',
                block_type: "task_v2",
                label: "research_task_1",
                child_workflow_run_block_id: childId,
              },
            ],
          },
          children: [],
          thought: null,
          created_at: "2026-06-10T00:00:00Z",
          modified_at: "2026-06-10T00:00:00Z",
        },
      ],
    });
    const setIntervalSpy = vi.spyOn(window, "setInterval");
    try {
      await renderChat();
      await submit("build a workflow");
      streamCalls[0]!.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        turn_index: 0,
        mode: "build",
        timestamp: "2026-06-10T00:00:00Z",
      });
      streamCalls[0]!.onMessage({
        type: "design_start",
        timestamp: "2026-06-10T00:00:00Z",
      });
      streamCalls[0]!.onMessage({
        type: "block_progress",
        workflow_run_id: "wr_1",
        workflow_run_block_id: "wrb_step",
        block_label: "research",
        block_type: "agent_step",
        status: "running",
        iteration: 0,
        timestamp: "2026-06-10T00:00:00Z",
      });
      streamCalls[0]!.onMessage({
        type: "block_progress",
        workflow_run_id: "wr_1",
        workflow_run_block_id: "wrb_child",
        block_label: "research_task_1",
        block_type: "task_v2",
        status: "running",
        iteration: 0,
        timestamp: "2026-06-10T00:00:02Z",
      });
      await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));
      const tick = setIntervalSpy.mock.calls.find((c) => c[1] === 2500)![0] as (
        ...args: unknown[]
      ) => void;
      tick();
      await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(2));

      await act(async () => pending[1]!(timeline("wrb_child")));
      await act(async () => pending[0]!(timeline(null)));

      await waitFor(() =>
        expect(
          screen.getAllByTestId("copilot-test-card")[0]!.textContent,
        ).toContain("Click Start"),
      );
      const cards = screen.getAllByTestId("copilot-test-card");
      expect(cards).toHaveLength(2);
      expect(
        cards[1]!.parentElement!.closest('[data-testid="copilot-test-card"]'),
      ).toBe(cards[0]);
    } finally {
      setIntervalSpy.mockRestore();
    }
  });

  it("nests a reloaded turn's child from its saved parent id and reads the calls with one timeline GET", async () => {
    seedReloadedAgentStepTurn();
    timelineGet.mockResolvedValue(reloadedAgentStepTimeline());
    await renderChat();
    await openReloadedStepCard();

    const cards = screen.getAllByTestId("copilot-test-card");
    expect(
      cards[1]!.parentElement!.closest('[data-testid="copilot-test-card"]'),
    ).toBe(cards[0]);
    await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));
    expect(timelineGet.mock.calls[0]![0]).toBe(
      "/workflows/wpid_1/runs/wr_1/timeline",
    );
    await waitFor(() =>
      expect(screen.getByText("Step call goal")).toBeTruthy(),
    );
    const child = screen.getAllByTestId("copilot-test-card")[1]!;
    // A child with action rows folds when finished; open it so its rows would be in the DOM.
    const childHeader = within(child).getAllByRole("button")[0]!;
    if (childHeader.getAttribute("aria-expanded") === "false") {
      fireEvent.click(childHeader);
    }
    expect(within(child).queryByText("Child action reasoning")).toBeNull();
  });

  it("re-reads the calls when the same chat re-hydrates after a read showed the step finished", async () => {
    seedReloadedAgentStepTurn();
    timelineGet.mockResolvedValue(reloadedAgentStepTimeline());
    const view = await renderChat();
    await openReloadedStepCard();
    await waitFor(() =>
      expect(screen.getByText("Step call goal")).toBeTruthy(),
    );
    const readsFor = (wpid: string) =>
      timelineGet.mock.calls.filter(
        ([url]) => url === `/workflows/${wpid}/runs/wr_1/timeline`,
      ).length;
    expect(readsFor("wpid_1")).toBe(1);

    routeParams.current = {
      ...routeParams.current,
      workflowPermanentId: "wpid_2",
    };
    view.rerender(chatUi());
    await waitFor(() =>
      expect(screen.getByText("Tested the workflow.")).toBeTruthy(),
    );
    routeParams.current = {
      ...routeParams.current,
      workflowPermanentId: "wpid_1",
    };
    view.rerender(chatUi());
    await waitFor(() =>
      expect(screen.getByText("Tested the workflow.")).toBeTruthy(),
    );
    await openReloadedStepCard();

    await waitFor(() => expect(readsFor("wpid_1")).toBe(2));
    await waitFor(() =>
      expect(screen.getByText("Step call goal")).toBeTruthy(),
    );
  });
  it("re-reads a step the turn stopped watching when its row reopens after the run ends", async () => {
    const call = (index: number, tool: string, status: string) => ({
      index,
      tool,
      status,
      started_at: `2026-06-10T00:00:0${index + 1}Z`,
      finished_at:
        status === "running" ? null : `2026-06-10T00:00:0${index + 2}Z`,
      arguments: JSON.stringify({ prompt: `Call ${index} goal` }),
    });
    const timeline = (status: string, calls: unknown[]) => ({
      data: [
        {
          type: "block",
          block: {
            workflow_run_block_id: "wrb_step",
            block_type: "agent_step",
            label: "research",
            status,
            actions: [],
            tool_calls: calls,
          },
          children: [],
          thought: null,
          created_at: "2026-06-10T00:00:00Z",
          modified_at: "2026-06-10T00:00:00Z",
        },
      ],
    });
    timelineGet.mockResolvedValue(
      timeline("running", [
        call(0, "page_goto", "completed"),
        call(1, "page_extract", "running"),
      ]),
    );

    await renderChat();
    await submit("build a workflow");
    streamCalls[0]!.onMessage({
      type: "turn_start",
      turn_id: "turn-1",
      turn_index: 0,
      mode: "build",
      timestamp: "2026-06-10T00:00:00Z",
    });
    streamCalls[0]!.onMessage({
      type: "design_start",
      timestamp: "2026-06-10T00:00:00Z",
    });
    streamCalls[0]!.onMessage(
      blockProgressFrame({
        workflow_run_id: "wr_1",
        workflow_run_block_id: "wrb_step",
        block_label: "research",
        block_type: "agent_step",
      }),
    );
    await waitFor(() => expect(screen.getByText("Call 1 goal")).toBeTruthy());
    streamCalls[0]!.onMessage(runOutcomeFrame({ verdict: "not_evaluated" }));
    await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(2));
    await act(async () => {});
    streamCalls[0]!.onMessage({
      type: "response",
      workflow_copilot_chat_id: "chat_1",
      message: "Ran out of time.",
      response_time: "2026-06-10T00:00:06Z",
      proposal_disposition: "no_proposal",
      turn_id: "turn-1",
      narrative_payload: null,
    });
    await act(async () => streamCalls[0]!.resolve());
    await waitFor(() =>
      expect(screen.getAllByText("Ran out of time.").length).toBeGreaterThan(0),
    );
    expect(timelineGet).toHaveBeenCalledTimes(2);

    timelineGet.mockResolvedValue(
      timeline("completed", [
        call(0, "page_goto", "completed"),
        call(1, "page_extract", "completed"),
        call(2, "finish", "completed"),
      ]),
    );
    const step = screen.getAllByTestId("copilot-test-card")[0]!;
    fireEvent.click(within(step).getAllByRole("button")[0]!);

    await waitFor(() => expect(screen.getByText("Call 2 goal")).toBeTruthy());
    expect(timelineGet).toHaveBeenCalledTimes(3);

    fireEvent.click(within(step).getAllByRole("button")[0]!);
    fireEvent.click(within(step).getAllByRole("button")[0]!);
    await act(async () => {});
    expect(timelineGet).toHaveBeenCalledTimes(3);
  });

  it("re-reads a later step whose card opens after the turn, even when an earlier read showed every step finished", async () => {
    const stepA = {
      id: "wrb_a",
      label: "step_a",
      status: "completed",
      calls: [agentStepCall(0, "completed", "Goal A0")],
    };
    timelineGet.mockResolvedValue(agentStepTimeline([stepA]));
    const setIntervalSpy = vi.spyOn(window, "setInterval");
    try {
      await startDesignTurn();
      streamCalls[0]!.onMessage(agentStepFrame("wrb_a", "step_a", "running"));
      await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));
      await act(async () => {});

      timelineGet.mockResolvedValue(
        agentStepTimeline([
          stepA,
          {
            id: "wrb_b",
            label: "step_b",
            status: "running",
            calls: [agentStepCall(0, "running", "Goal B0")],
          },
        ]),
      );
      streamCalls[0]!.onMessage(agentStepFrame("wrb_a", "step_a", "completed"));
      streamCalls[0]!.onMessage(agentStepFrame("wrb_b", "step_b", "running"));
      const tick = setIntervalSpy.mock.calls.find((c) => c[1] === 2500)![0] as (
        ...args: unknown[]
      ) => void;
      tick();
      await waitFor(() => expect(screen.getByText("Goal B0")).toBeTruthy());
      await endTurnWithRunLive();
      expect(timelineGet).toHaveBeenCalledTimes(2);

      timelineGet.mockResolvedValue(
        agentStepTimeline([
          stepA,
          {
            id: "wrb_b",
            label: "step_b",
            status: "completed",
            calls: [
              agentStepCall(0, "completed", "Goal B0"),
              agentStepCall(1, "completed", "Goal B1"),
            ],
          },
        ]),
      );
      const fold = screen.queryByRole("button", { name: /^Worked through/ });
      if (fold !== null) fireEvent.click(fold);
      fireEvent.click(screen.getByTitle("Highlight step_b on canvas"));

      await waitFor(() => expect(screen.getByText("Goal B1")).toBeTruthy());
      expect(timelineGet).toHaveBeenCalledTimes(3);
    } finally {
      setIntervalSpy.mockRestore();
    }
  });

  it("re-reads an open step card when its activity row reopens after the run ends", async () => {
    const step = (status: string, calls: unknown[]) =>
      agentStepTimeline([{ id: "wrb_step", label: "research", status, calls }]);
    timelineGet.mockResolvedValue(
      step("running", [
        agentStepCall(0, "completed", "Call 0 goal"),
        agentStepCall(1, "running", "Call 1 goal"),
      ]),
    );
    await startDesignTurn();
    streamCalls[0]!.onMessage({
      type: "tool_call",
      tool_name: "run_blocks_and_collect_debug",
      display_label: "Testing workflow",
      tool_input: {},
      iteration: 0,
      tool_call_id: "call_test",
      timestamp: "2026-06-10T00:00:00Z",
    });
    streamCalls[0]!.onMessage(
      agentStepFrame("wrb_step", "research", "running"),
    );
    await waitFor(() => expect(screen.getByText("Call 1 goal")).toBeTruthy());
    await endTurnWithRunLive();

    const fold = screen.queryByRole("button", { name: /^Worked through/ });
    if (fold !== null) fireEvent.click(fold);
    const row = document.querySelector<HTMLElement>(
      '[data-tool-call-id="call_test"] > [data-activity-line="true"]',
    )!;
    if (row.getAttribute("aria-expanded") === "false") fireEvent.click(row);
    const header = () => screen.getByTitle("Highlight research on canvas");
    if (header().getAttribute("aria-expanded") === "false") {
      fireEvent.click(header());
    }
    await waitFor(() => expect(screen.getByText("Call 1 goal")).toBeTruthy());
    await act(async () => {});
    const readsBeforeReopen = timelineGet.mock.calls.length;
    fireEvent.click(row);
    expect(screen.queryByText("Call 1 goal")).toBeNull();

    timelineGet.mockResolvedValue(
      step("completed", [
        agentStepCall(0, "completed", "Call 0 goal"),
        agentStepCall(1, "completed", "Call 1 goal"),
        agentStepCall(2, "completed", "Call 2 goal"),
      ]),
    );
    fireEvent.click(row);

    await waitFor(() => expect(screen.getByText("Call 2 goal")).toBeTruthy());
    expect(timelineGet).toHaveBeenCalledTimes(readsBeforeReopen + 1);
  });
});

describe("WorkflowCopilotChat — studio run focus", () => {
  it("focuses the dispatched run from run_started and does not re-navigate on block_progress", async () => {
    await renderChat(makeDockedProps());
    await submit("build a workflow");

    streamCalls[0]!.onMessage(runStartedFrame());
    await waitFor(() => expect(switchStudioRun).toHaveBeenCalledTimes(1));
    expect(switchStudioRun).toHaveBeenCalledWith("wr_1");

    streamCalls[0]!.onMessage(blockProgressFrame({ workflow_run_id: "wr_1" }));
    await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));
    expect(switchStudioRun).toHaveBeenCalledTimes(1);
  });
});

const toolCallFrame = (overrides: Partial<Record<string, unknown>> = {}) => ({
  type: "tool_call",
  tool_name: "run_browser_code",
  display_label: "Working in the browser",
  tool_input: {},
  iteration: 0,
  tool_call_id: "call_1",
  timestamp: "2026-06-10T00:00:00Z",
  browser_session_id: "pbs_debug",
  ...overrides,
});

describe("WorkflowCopilotChat — Copilot's own browser in the studio pane", () => {
  beforeEach(() =>
    useStudioBrowserStore.setState({ copilotTurnActive: false }),
  );
  afterEach(() => {
    window.history.replaceState(null, "", "/");
    navigate.mockReset();
  });

  it("pins the pane to the browser a tool call acts in, replacing the entry", async () => {
    window.history.replaceState(null, "", "/?wr=wr_1&wrs=copilot");
    await renderChat(makeDockedProps());
    await submit("build a workflow");

    streamCalls[0]!.onMessage(toolCallFrame());

    expect(navigate).toHaveBeenCalledWith(
      expect.objectContaining({ search: "?wr=wr_1&wrs=pbs_debug" }),
      expect.objectContaining({ replace: true }),
    );
  });

  it("moves the pin to the run's browser when a later call names it", async () => {
    navigate.mockImplementation((to: { search: string }) =>
      window.history.replaceState(null, "", `/${to.search}`),
    );
    window.history.replaceState(null, "", "/?wr=wr_1&wrs=copilot");
    await renderChat(makeDockedProps());
    await submit("build a workflow");

    streamCalls[0]!.onMessage(toolCallFrame());
    streamCalls[0]!.onMessage(
      toolCallFrame({ tool_call_id: "call_2", browser_session_id: "pbs_run" }),
    );

    expect(window.location.search).toBe("?wr=wr_1&wrs=pbs_run");
  });

  it.each([
    ["undocked", {}, "?wr=wr_1&wrs=copilot", {}],
    [
      "carrying no browser id",
      makeDockedProps(),
      "?wr=wr_1&wrs=copilot",
      { browser_session_id: null },
    ],
    ["on a run the user opened", makeDockedProps(), "?wr=wr_user", {}],
  ])(
    "leaves the URL alone for a tool call %s",
    async (_case, props, search, frame) => {
      window.history.replaceState(null, "", `/${search}`);
      await renderChat(props);
      await submit("build a workflow");

      streamCalls[0]!.onMessage(toolCallFrame(frame));

      expect(navigate).not.toHaveBeenCalled();
    },
  );

  it("drops a pin an earlier turn left in the URL when a new turn starts", async () => {
    window.history.replaceState(null, "", "/?wr=wr_1&wrs=pbs_debug");
    await renderChat(makeDockedProps());
    expect(navigate).not.toHaveBeenCalled();

    await submit("build a workflow");

    expect(navigate).toHaveBeenCalledWith(
      expect.objectContaining({ search: "?wr=wr_1&wrs=copilot" }),
      expect.objectContaining({ replace: true }),
    );
  });

  it("tells the pane a docked turn is active only while it runs", async () => {
    await renderChat(makeDockedProps());
    expect(useStudioBrowserStore.getState().copilotTurnActive).toBe(false);

    await submit("build a workflow");
    expect(useStudioBrowserStore.getState().copilotTurnActive).toBe(true);

    await act(async () => {
      streamCalls[0]!.resolve();
    });
    await waitFor(() =>
      expect(useStudioBrowserStore.getState().copilotTurnActive).toBe(false),
    );
  });

  it("clears the active turn when the chat unmounts mid-turn", async () => {
    const view = await renderChat(makeDockedProps());
    await submit("build a workflow");
    expect(useStudioBrowserStore.getState().copilotTurnActive).toBe(true);

    view.unmount();

    expect(useStudioBrowserStore.getState().copilotTurnActive).toBe(false);
  });

  it("never reports an undocked turn to the pane", async () => {
    await renderChat({});
    await submit("build a workflow");
    expect(useStudioBrowserStore.getState().copilotTurnActive).toBe(false);
  });
});

describe("WorkflowCopilotChat — Editor returns after the turn's run", () => {
  it("reopens Editor once when the focused run's terminal outcome lands", async () => {
    await renderChat({ ...makeDockedProps(), workflowRunId: "wr_1" });
    await submit("build a workflow");
    streamCalls[0]!.onMessage(runStartedFrame());
    streamCalls[0]!.onMessage(runOutcomeFrame());
    expect(reopenEditor).not.toHaveBeenCalled();

    streamCalls[0]!.onMessage(runOutcomeFrame({ verdict: "demonstrated" }));
    streamCalls[0]!.onMessage(runOutcomeFrame({ verdict: "demonstrated" }));
    expect(reopenEditor).toHaveBeenCalledTimes(1);
  });

  it("reopens Editor once the focus lands when the run ended before it did", async () => {
    const props = makeDockedProps();
    const view = await renderChat(props);
    await submit("build a workflow");
    streamCalls[0]!.onMessage(runStartedFrame());
    streamCalls[0]!.onMessage(runOutcomeFrame({ verdict: "demonstrated" }));
    expect(reopenEditor).not.toHaveBeenCalled();
    view.rerender(chatUi({ ...props, workflowRunId: "wr_1" }));
    expect(reopenEditor).toHaveBeenCalledTimes(1);
  });

  it("leaves the panes alone when the user has switched to another run", async () => {
    const props = { ...makeDockedProps(), workflowRunId: "wr_1" };
    const view = await renderChat(props);
    await submit("build a workflow");
    streamCalls[0]!.onMessage(runStartedFrame());
    view.rerender(chatUi({ ...props, workflowRunId: "wr_2" }));
    streamCalls[0]!.onMessage(runOutcomeFrame({ verdict: "demonstrated" }));
    focusedRun.current = { workflow_run_id: "wr_1", status: "completed" };
    view.rerender(chatUi({ ...props, workflowRunId: "wr_1" }));
    expect(reopenEditor).not.toHaveBeenCalled();
  });

  it("does not reopen Editor when the user returns to a run that ended while they were away", async () => {
    const props = { ...makeDockedProps(), workflowRunId: "wr_1" };
    const view = await renderChat(props);
    await submit("build a workflow");
    streamCalls[0]!.onMessage(runStartedFrame());
    view.rerender(chatUi({ ...props, workflowRunId: "wr_2" }));
    await act(async () => {
      streamCalls[0]!.reject(new Error("network"));
    });
    focusedRun.current = { workflow_run_id: "wr_1", status: "completed" };
    view.rerender(chatUi({ ...props, workflowRunId: "wr_1" }));
    await act(async () => {});
    expect(reopenEditor).not.toHaveBeenCalled();
  });

  it("reopens Editor from the run status when the stream is severed first", async () => {
    await renderChat({ ...makeDockedProps(), workflowRunId: "wr_1" });
    await submit("build a workflow");
    streamCalls[0]!.onMessage(runStartedFrame());
    focusedRun.current = { workflow_run_id: "wr_1", status: "completed" };
    await act(async () => {
      streamCalls[0]!.reject(new Error("network"));
    });
    await waitFor(() => expect(reopenEditor).toHaveBeenCalledTimes(1));
  });
});

describe("WorkflowCopilotChat — build follow", () => {
  const focusBlock = vi.fn();

  beforeEach(() => {
    focusBlock.mockClear();
    useWorkflowBlockSearchStore.getState().registerHandle({
      getTargets: () => [
        { nodeId: "node_login", label: "login", blockType: "task" },
      ],
      focusBlock,
    });
    window.history.pushState(null, "", "/?panes=copilot,editor");
  });

  afterEach(() => {
    useWorkflowBlockSearchStore.getState().registerHandle(null);
    window.history.pushState(null, "", "/");
  });

  it("focuses the canvas on the block a progress frame names", async () => {
    await renderChat(makeDockedProps());
    await submit("build it");

    streamCalls[0]!.onMessage(
      blockProgressFrame({ block_label: "login", status: "running" }),
    );

    expect(focusBlock).toHaveBeenCalledWith("node_login");
  });

  it("follows a block only once per label", async () => {
    await renderChat(makeDockedProps());
    await submit("build it");

    streamCalls[0]!.onMessage(
      blockProgressFrame({ block_label: "login", status: "running" }),
    );
    streamCalls[0]!.onMessage(
      blockProgressFrame({ block_label: "login", status: "completed" }),
    );

    expect(focusBlock).toHaveBeenCalledTimes(1);
  });

  it("stops following after the user presses outside the copilot pane", async () => {
    await renderChat(makeDockedProps());
    await submit("build it");

    fireEvent.pointerDown(document.body);
    streamCalls[0]!.onMessage(
      blockProgressFrame({ block_label: "login", status: "running" }),
    );

    expect(focusBlock).not.toHaveBeenCalled();
  });

  it("does not follow when the editor pane is closed", async () => {
    window.history.pushState(null, "", "/?panes=copilot");
    await renderChat(makeDockedProps());
    await submit("build it");

    streamCalls[0]!.onMessage(
      blockProgressFrame({ block_label: "login", status: "running" }),
    );

    expect(focusBlock).not.toHaveBeenCalled();
  });
});
