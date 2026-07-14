import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  FeatureFlagContext,
  FeatureFlagValueContext,
} from "@/hooks/useFeatureFlag";

type StreamBody = { message: string; workflow_run_id?: string | null };
type StreamCall = {
  body: StreamBody;
  onMessage: (payload: unknown) => boolean;
  resolve: () => void;
  reject: (error: unknown) => void;
};

const { mockCopilotUxV1Enabled } = vi.hoisted(() => ({
  mockCopilotUxV1Enabled: vi.fn(),
}));

vi.mock("posthog-js/react", () => ({
  useFeatureFlagEnabled: () => mockCopilotUxV1Enabled(),
}));

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

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return {
    ...actual,
    useParams: () => routeParams.current,
    useSearchParams: () => [new URLSearchParams(), vi.fn()],
  };
});

// Unrelated to this file's tests; the real hook needs a QueryClientProvider
// this harness doesn't set up.
vi.mock("../hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: undefined }),
}));

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

vi.mock("@/store/WorkflowHasChangesStore", () => ({
  useWorkflowHasChangesStore: () => ({ getSaveData: () => saveData }),
}));

import { WorkflowCopilotChat } from "./WorkflowCopilotChat";

const BOOLEAN_FLAGS: Record<string, boolean> = {
  ENABLE_WORKFLOW_COPILOT_V2: true,
  WORKFLOW_COPILOT_CODE_BLOCK_MODE: false,
  CODE_BLOCK_ACCESS: false,
};

function chatUi() {
  return (
    <FeatureFlagContext.Provider value={(name) => BOOLEAN_FLAGS[name]}>
      <FeatureFlagValueContext.Provider value={() => undefined}>
        <WorkflowCopilotChat />
      </FeatureFlagValueContext.Provider>
    </FeatureFlagContext.Provider>
  );
}

async function renderChat() {
  const view = render(chatUi());
  await waitFor(() =>
    expect(
      screen.getByPlaceholderText(
        /Message Skyvern Copilot|Ask Copilot to build/,
      ),
    ).toBeTruthy(),
  );
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

beforeEach(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  mockCopilotUxV1Enabled.mockReset();
  mockCopilotUxV1Enabled.mockReturnValue(true);
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
});

describe("WorkflowCopilotChat — recorded-action fetch wiring", () => {
  it("fetches the run timeline exactly once when a run_outcome frame arrives", async () => {
    await renderChat();
    await submit("build a workflow");

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "" } });
    streamCalls[0]!.onMessage(runOutcomeFrame());

    await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));
    expect(timelineGet.mock.calls[0]![0]).toBe(
      "/workflows/wpid_1/runs/wr_1/timeline",
    );
  });

  it("does not re-fetch for a second run_outcome frame carrying the same run id", async () => {
    await renderChat();
    await submit("build a workflow");

    streamCalls[0]!.onMessage(runOutcomeFrame({ verdict: "evaluating" }));
    await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));

    streamCalls[0]!.onMessage(runOutcomeFrame({ verdict: "demonstrated" }));
    // Give any accidental second fetch a chance to fire before asserting.
    await waitFor(() => expect(timelineGet).toHaveBeenCalledTimes(1));
  });

  it("never fetches when the run_outcome frame carries an empty workflow_run_id", async () => {
    await renderChat();
    await submit("build a workflow");

    streamCalls[0]!.onMessage(runOutcomeFrame({ workflow_run_id: "" }));

    expect(timelineGet).not.toHaveBeenCalled();
  });

  it("never fetches the run timeline when copilot_ux_v1 is off", async () => {
    mockCopilotUxV1Enabled.mockReturnValue(false);
    await renderChat();
    await submit("build a workflow");

    streamCalls[0]!.onMessage(runOutcomeFrame());

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

    // A settled turn's card defaults to its rolled-up summary; expand it,
    // then expand the block row, to reach the replay the fetch just patched
    // in — this is the reviewer's "does the verify card ever receive it".
    const statusRegion = await waitFor(() => screen.getByRole("status"));
    fireEvent.click(
      within(statusRegion).getByRole("button", { expanded: false }),
    );
    // uxV1 is on by default in this file, so the row's primary text is the
    // humanized label ("block_1" -> "Block 1"), not the raw block label.
    fireEvent.click(within(statusRegion).getByText("Block 1"));

    await waitFor(() => expect(screen.getByText("Wobble Gizmo")).toBeTruthy());
  });
});
