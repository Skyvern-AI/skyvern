import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FeatureFlagContext } from "@/hooks/useFeatureFlag";
import { useRecordingRefinementEvidenceStore } from "@/store/RecordingRefinementEvidenceStore";

type StreamBody = {
  cancel_token?: string | null;
  code_block?: boolean | null;
  message: string;
  mode?: string | null;
  workflow_run_id?: string | null;
  product_action?: string | null;
  recording_evidence?: unknown;
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
  getSseClient,
  cancelPost,
  historyGet,
  historyResponse,
  routeParams,
  toast,
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
  const getStreamingClient = vi
    .fn()
    .mockResolvedValue({ postStreaming: streaming });
  const history: {
    data: {
      workflow_copilot_chat_id: string | null;
      request_turn_id?: string | null;
      chat_history: unknown[];
      proposed_workflow: Record<string, unknown> | null;
      auto_accept: boolean;
    };
  } = {
    data: {
      workflow_copilot_chat_id: null as string | null,
      chat_history: [] as unknown[],
      proposed_workflow: null as Record<string, unknown> | null,
      auto_accept: false,
    },
  };
  const get = vi.fn().mockImplementation(() => Promise.resolve(history));
  const toastFn = vi.fn();
  const params = {
    current: {
      workflowPermanentId: "wpid_1",
      workflowRunId: undefined as string | undefined,
    },
  };
  return {
    streamCalls: calls,
    postStreaming: streaming,
    getSseClient: getStreamingClient,
    cancelPost: post,
    historyGet: get,
    historyResponse: history,
    routeParams: params,
    toast: toastFn,
  };
});

vi.mock("@/api/sse", () => ({
  getSseClient,
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn().mockResolvedValue({
    get: historyGet,
    post: cancelPost,
  }),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

vi.mock("@/components/ui/use-toast", () => ({ toast }));

vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return {
    ...actual,
    useParams: () => routeParams.current,
    useSearchParams: () => [new URLSearchParams(), vi.fn()],
    useNavigate: () => vi.fn(),
    useLocation: () => ({
      pathname: "/",
      search: "",
      hash: "",
      state: null,
      key: "default",
    }),
  };
});

vi.mock("posthog-js/react", () => ({
  useFeatureFlagEnabled: () => true,
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
    pinSavedSessionIp: false,
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
  useWorkflowHasChangesStore: Object.assign(
    () => ({ getSaveData: () => saveData }),
    { getState: () => ({ hasChanges: false }) },
  ),
}));

// Unrelated to this file's tests; the real hook needs a QueryClientProvider
// this harness doesn't set up.
vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: undefined }),
}));

import { WorkflowCopilotChat } from "./WorkflowCopilotChat";
import type { CopilotProductAction } from "./workflowCopilotTypes";

const BOOLEAN_FLAGS: Record<string, boolean> = {
  WORKFLOW_COPILOT_CODE_BLOCK_MODE: false,
  CODE_BLOCK_ACCESS: false,
};

type ChatProps = {
  workflowRunId?: string | null;
  initialAction?: CopilotProductAction;
  requiresLiveBrowser?: boolean;
  isLiveBrowserReady?: boolean;
  liveBrowserSessionId?: string | null;
  onInitialMessageConsumed?: () => void;
};

function chatUi(props: ChatProps) {
  return (
    <FeatureFlagContext.Provider value={(name) => BOOLEAN_FLAGS[name]}>
      <WorkflowCopilotChat {...props} />
    </FeatureFlagContext.Provider>
  );
}

async function renderChat(props: ChatProps = {}) {
  const view = render(chatUi(props));
  await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
  return view;
}

async function submit(value: string) {
  fireEvent.change(screen.getByRole("textbox"), { target: { value } });
  await act(async () => {
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
  });
}

beforeEach(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  streamCalls.length = 0;
  postStreaming.mockClear();
  getSseClient.mockReset();
  getSseClient.mockResolvedValue({ postStreaming });
  cancelPost.mockClear();
  cancelPost.mockResolvedValue({});
  historyGet.mockReset();
  historyGet.mockImplementation(() => Promise.resolve(historyResponse));
  toast.mockClear();
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
  BOOLEAN_FLAGS.WORKFLOW_COPILOT_CODE_BLOCK_MODE = false;
  BOOLEAN_FLAGS.CODE_BLOCK_ACCESS = false;
  useRecordingRefinementEvidenceStore.setState({ armed: null });
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

describe("WorkflowCopilotChat — run grounding bridge", () => {
  it("sends the workflowRunId prop as workflow_run_id when the route param is absent (studio ?wr= bridge)", async () => {
    await renderChat({ workflowRunId: "wr_prop_123" });
    await submit("this run failed, fix it");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(streamCalls[0]?.body.workflow_run_id).toBe("wr_prop_123");
  });

  it("prefers the prop over the route param", async () => {
    routeParams.current = {
      workflowPermanentId: "wpid_1",
      workflowRunId: "wr_route",
    };
    await renderChat({ workflowRunId: "wr_prop_123" });
    await submit("this run failed, fix it");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(streamCalls[0]?.body.workflow_run_id).toBe("wr_prop_123");
  });

  it("opens a diagnose_run turn from a typed action, with no user bubble", async () => {
    await renderChat({
      workflowRunId: "wr_prop_123",
      initialAction: {
        kind: "diagnose_run",
        workflowRunId: "wr_clicked",
        nonce: "n1",
      },
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(streamCalls[0]?.body.product_action).toBe("diagnose_run");
    expect(streamCalls[0]?.body.workflow_run_id).toBe("wr_clicked");
    expect(streamCalls[0]?.body.message.trim()).not.toBe("");
    const echoed = screen.getByText(streamCalls[0]!.body.message);
    expect(echoed.closest('[role="status"]')).not.toBeNull();
  });

  it("keeps refinement evidence until the server accepts the turn", async () => {
    const evidence = {
      schema_version: 1,
      recording: { browser_session_id: "pbs-1" },
      actions: [{ action_id: "a001" }],
      deleted_action_ids: [],
      truncated_action_count: 0,
      provenance: { source: "browser_recording" },
    };
    useRecordingRefinementEvidenceStore
      .getState()
      .set({ nonce: "n-refine", evidence });
    const consumed = vi.fn();

    await renderChat({
      initialAction: { kind: "refine_recording", nonce: "n-refine" },
      onInitialMessageConsumed: consumed,
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(streamCalls[0]?.body.recording_evidence).toEqual(evidence);
    expect(useRecordingRefinementEvidenceStore.getState().armed).not.toBeNull();
    expect(consumed).not.toHaveBeenCalled();

    await act(async () => {
      streamCalls[0]?.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        turn_index: 0,
        mode: "build",
        timestamp: "2026-06-10T00:00:00Z",
      });
    });

    expect(useRecordingRefinementEvidenceStore.getState().armed).toBeNull();
    expect(consumed).toHaveBeenCalledTimes(1);
  });

  it("keeps recording refinement progress visible through completion", async () => {
    useRecordingRefinementEvidenceStore.getState().set({
      nonce: "n-refine",
      evidence: {
        schema_version: 1,
        recording: { browser_session_id: "pbs-1" },
        actions: [{ action_id: "a001" }, { action_id: "a002" }],
        deleted_action_ids: [],
        truncated_action_count: 0,
        provenance: { source: "browser_recording" },
      },
    });

    await renderChat({
      initialAction: { kind: "refine_recording", nonce: "n-refine" },
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(
      screen.getByText("Turning your task demonstration into a workflow"),
    ).toBeTruthy();
    expect(screen.getByText("Task recording saved")).toBeTruthy();
    expect(screen.getByText("Browser interactions prepared")).toBeTruthy();
    expect(screen.getByText("Refining the workflow…")).toBeTruthy();

    await act(async () => {
      streamCalls[0]?.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        turn_index: 0,
        mode: "build",
        timestamp: "2026-06-10T00:00:00Z",
      });
    });

    expect(
      screen.getByText("Turning your task demonstration into a workflow"),
    ).toBeTruthy();

    await act(async () => {
      streamCalls[0]?.onMessage({
        type: "response",
        turn_id: "turn-1",
        message: "The recorded workflow is ready.",
        workflow_copilot_chat_id: "chat-1",
        response_time: "2026-06-10T00:00:02Z",
        updated_workflow: { workflow_id: "wf-draft" },
        proposal_disposition: "review_required",
      });
      streamCalls[0]?.resolve();
    });

    expect(screen.getByText("Workflow ready to review")).toBeTruthy();
    expect(screen.getByText("Refined into a reusable workflow")).toBeTruthy();
  });

  it("keeps recording refinement active while a disconnected stream recovers", async () => {
    useRecordingRefinementEvidenceStore.getState().set({
      nonce: "n-refine",
      evidence: {
        schema_version: 1,
        recording: { browser_session_id: "pbs-1" },
        actions: [{ action_id: "a001" }],
        deleted_action_ids: [],
        truncated_action_count: 0,
        provenance: { source: "browser_recording" },
      },
    });

    await renderChat({
      initialAction: { kind: "refine_recording", nonce: "n-refine" },
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    vi.useFakeTimers();

    await act(async () => {
      streamCalls[0]?.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        turn_index: 0,
        mode: "build",
        timestamp: "2026-06-10T00:00:00Z",
      });
      streamCalls[0]?.reject(new Error("stream disconnected"));
    });

    expect(
      screen.getByText("Turning your task demonstration into a workflow"),
    ).toBeTruthy();
    expect(screen.queryByText("Workflow refinement stopped")).toBeNull();

    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      chat_history: [
        {
          sender: "product",
          content: "Refine the recording (1 actions) into a reusable workflow.",
          turn_id: "turn-1",
          created_at: "2026-06-10T00:00:00Z",
        },
        {
          sender: "ai",
          content: "This turn was interrupted before it could finish.",
          created_at: "2026-06-10T00:00:02Z",
          turn_outcome: {
            copilot_turn_id: "turn-1",
            terminal_reason: "interrupted",
          },
        },
      ],
      proposed_workflow: null,
      auto_accept: false,
    };
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });

    expect(
      screen.getByText("Turning your task demonstration into a workflow"),
    ).toBeTruthy();
    expect(screen.queryByText("Workflow ready to review")).toBeNull();

    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      chat_history: [
        {
          sender: "product",
          content: "Refine the recording (1 actions) into a reusable workflow.",
          turn_id: "turn-1",
          created_at: "2026-06-10T00:00:00Z",
        },
        {
          sender: "ai",
          content: "The recorded workflow is ready.",
          created_at: "2026-06-10T00:00:03Z",
          turn_outcome: {
            copilot_turn_id: "turn-1",
            terminal_reason: "completed",
          },
          narrative_payload: {
            turnId: "turn-1",
            proposalDisposition: "review_untested",
            draft: {
              blockCount: 1,
              blockLabels: ["Open page"],
              summary: "Built from the recording",
            },
            terminal: "response",
          },
        },
      ],
      proposed_workflow: { workflow_id: "wf-draft" },
      auto_accept: false,
    };
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });

    expect(screen.getByText("Workflow ready to review")).toBeTruthy();
    expect(screen.getByText("Refined into a reusable workflow")).toBeTruthy();
  });

  it("recovers recording refinement when the turn_start frame is lost", async () => {
    const consumed = vi.fn();
    useRecordingRefinementEvidenceStore.getState().set({
      nonce: "n-refine",
      evidence: {
        schema_version: 1,
        recording: { browser_session_id: "pbs-1" },
        actions: [{ action_id: "a001" }],
        deleted_action_ids: [],
        truncated_action_count: 0,
        provenance: { source: "browser_recording" },
      },
    });
    await renderChat({
      initialAction: { kind: "refine_recording", nonce: "n-refine" },
      onInitialMessageConsumed: consumed,
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    vi.useFakeTimers();
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      request_turn_id: null,
      chat_history: [
        {
          sender: "product",
          content: "Refine the recording (1 actions) into a reusable workflow.",
          turn_id: "turn-earlier-same-receipt",
          created_at: new Date().toISOString(),
        },
      ],
      proposed_workflow: null,
      auto_accept: false,
    };

    await act(async () => {
      streamCalls[0]?.reject(new Error("stream disconnected"));
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(historyGet).toHaveBeenCalledWith(
      "/workflow/copilot/chat-history",
      expect.objectContaining({
        params: {
          request_cancel_token: streamCalls[0]?.body.cancel_token,
          workflow_permanent_id: "wpid_1",
        },
      }),
    );

    expect(
      screen.getByText("Turning your task demonstration into a workflow"),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "The connection dropped, so Copilot is checking whether this turn finished.",
      ),
    ).toBeTruthy();

    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      request_turn_id: "turn-lost-start",
      chat_history: [
        {
          sender: "product",
          content: "Refine the recording (1 actions) into a reusable workflow.",
          turn_id: "turn-lost-start",
          created_at: new Date().toISOString(),
        },
      ],
      proposed_workflow: null,
      auto_accept: false,
    };
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(
      historyGet.mock.calls.filter(
        ([, config]) => config?.params?.request_cancel_token,
      ),
    ).toHaveLength(2);
    expect(useRecordingRefinementEvidenceStore.getState().armed).toBeNull();
    expect(consumed).toHaveBeenCalledTimes(1);

    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      request_turn_id: "turn-lost-start",
      chat_history: [
        historyResponse.data.chat_history[0]!,
        {
          sender: "ai",
          content: "The recorded workflow is ready.",
          created_at: new Date().toISOString(),
          turn_outcome: {
            copilot_turn_id: "turn-lost-start",
            terminal_reason: "completed",
          },
          narrative_payload: {
            turnId: "turn-lost-start",
            proposalDisposition: "review_untested",
            draft: {
              blockCount: 1,
              blockLabels: ["Open page"],
            },
            terminal: "response",
          },
        },
      ],
      proposed_workflow: null,
      auto_accept: false,
    };
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });

    expect(screen.getByText("Workflow refinement complete")).toBeTruthy();
  });

  it("scopes lost-turn recovery to the chat that sent the request", async () => {
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-existing",
      chat_history: [],
      proposed_workflow: null,
      auto_accept: false,
    };
    useRecordingRefinementEvidenceStore.getState().set({
      nonce: "n-refine",
      evidence: {
        schema_version: 1,
        recording: { browser_session_id: "pbs-1" },
        actions: [{ action_id: "a001" }],
        deleted_action_ids: [],
        truncated_action_count: 0,
        provenance: { source: "browser_recording" },
      },
    });
    await renderChat({
      initialAction: { kind: "refine_recording", nonce: "n-refine" },
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    historyGet.mockClear();
    vi.useFakeTimers();
    historyResponse.data.chat_history = [
      {
        sender: "product",
        content: "Refine the recording (1 actions) into a reusable workflow.",
        turn_id: "turn-lost-start",
        created_at: new Date().toISOString(),
      },
    ];

    await act(async () => {
      streamCalls[0]?.reject(new Error("stream disconnected"));
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(historyGet).toHaveBeenCalledWith(
      "/workflow/copilot/chat-history",
      expect.objectContaining({
        params: {
          request_cancel_token: streamCalls[0]?.body.cancel_token,
          workflow_copilot_chat_id: "chat-existing",
        },
      }),
    );
  });

  it("bounds a stalled lookup after losing turn_start", async () => {
    useRecordingRefinementEvidenceStore.getState().set({
      nonce: "n-refine",
      evidence: {
        schema_version: 1,
        recording: { browser_session_id: "pbs-1" },
        actions: [{ action_id: "a001" }],
        deleted_action_ids: [],
        truncated_action_count: 0,
        provenance: { source: "browser_recording" },
      },
    });
    await renderChat({
      initialAction: { kind: "refine_recording", nonce: "n-refine" },
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    historyGet.mockImplementation(
      (_path: string, config?: { signal?: AbortSignal }) =>
        new Promise((_resolve, reject) => {
          config?.signal?.addEventListener("abort", () =>
            reject(new Error("history lookup aborted")),
          );
        }),
    );
    vi.useFakeTimers();

    await act(async () => {
      streamCalls[0]?.reject(new Error("stream disconnected"));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(
      screen.getByText(
        "The connection dropped, so Copilot is checking whether this turn finished.",
      ),
    ).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500_000);
    });

    expect(screen.getByText("Workflow refinement stopped")).toBeTruthy();
    expect(
      screen.getByText("Sorry, I encountered an error. Please try again."),
    ).toBeTruthy();
  });

  it("only fails the recovery notice owned by the poll that gives up", async () => {
    await renderChat();
    vi.useFakeTimers();

    await submit("First request");
    await act(async () => {
      streamCalls[0]?.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        turn_index: 0,
        mode: "build",
        timestamp: "2026-06-10T00:00:00Z",
      });
      streamCalls[0]?.reject(new Error("stream disconnected"));
    });
    await submit("Second request");
    await act(async () => {
      streamCalls[1]?.onMessage({
        type: "turn_start",
        turn_id: "turn-2",
        turn_index: 1,
        mode: "build",
        timestamp: "2026-06-10T00:00:01Z",
      });
      streamCalls[1]?.reject(new Error("stream disconnected"));
    });

    const emptyHistory = {
      data: {
        workflow_copilot_chat_id: "chat-1",
        chat_history: [],
        proposed_workflow: null,
        auto_accept: false,
      },
    };
    historyGet.mockReset();
    for (let cycle = 0; cycle < 3; cycle += 1) {
      historyGet.mockRejectedValueOnce(new Error("turn-1 offline"));
      historyGet.mockResolvedValueOnce(emptyHistory);
    }
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000 + 3_000 + 5_000);
    });

    expect(
      screen.getAllByText(
        "The connection dropped, so Copilot is checking whether this turn finished.",
      ),
    ).toHaveLength(1);
    expect(
      screen.getAllByText("Sorry, I encountered an error. Please try again."),
    ).toHaveLength(1);
  });

  it("hydrates recording refinement progress from turn-correlated history", async () => {
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      chat_history: [
        {
          sender: "product",
          content: "Refine the recording (3 actions) into a reusable workflow.",
          turn_id: "turn-refine",
          created_at: "2026-06-10T00:00:00Z",
        },
        {
          sender: "user",
          content: "Add a final confirmation step.",
          turn_id: "turn-later",
          created_at: "2026-06-10T00:00:01Z",
        },
        {
          sender: "ai",
          content: "Added the confirmation step.",
          turn_id: "turn-later",
          created_at: "2026-06-10T00:00:02Z",
          turn_outcome: {
            copilot_turn_id: "turn-later",
            terminal_reason: "completed",
          },
        },
        {
          sender: "ai",
          content: "The recorded workflow is ready.",
          created_at: "2026-06-10T00:00:03Z",
          turn_outcome: {
            copilot_turn_id: "turn-refine",
            terminal_reason: "completed",
          },
          narrative_payload: {
            turnId: "turn-refine",
            proposalDisposition: "review_untested",
            draft: {
              blockCount: 3,
              blockLabels: ["Open page", "Enter search", "Select result"],
              summary: "Built from the recording",
            },
            terminal: "response",
          },
        },
      ],
      proposed_workflow: null,
      auto_accept: false,
    };

    await renderChat();

    expect(screen.getByText("Workflow refinement complete")).toBeTruthy();
    expect(screen.getByText("Refined into a reusable workflow")).toBeTruthy();
    expect(
      screen.queryByText(
        "Review the changes below, then save when you’re ready.",
      ),
    ).toBeNull();
    expect(screen.getByTestId("recording-refinement-progress")).toBeTruthy();
  });

  it("hydrates an agent-loop cancellation as cancelled", async () => {
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      chat_history: [
        {
          sender: "product",
          content: "Refine the recording (2 actions) into a reusable workflow.",
          turn_id: "turn-refine",
          created_at: "2026-06-10T00:00:00Z",
        },
        {
          sender: "ai",
          content: "The refinement was cancelled.",
          created_at: "2026-06-10T00:00:01Z",
          turn_outcome: {
            copilot_turn_id: "turn-refine",
            terminal_reason: "cancel",
          },
          narrative_payload: {
            turnId: "turn-refine",
            proposalDisposition: "review_untested",
            draft: {
              blockCount: 2,
              blockLabels: ["Open page", "Enter search"],
            },
            terminal: "response",
            cancelled: true,
          },
        },
      ],
      proposed_workflow: null,
      auto_accept: false,
    };

    await renderChat();

    expect(screen.getByText("Workflow refinement stopped")).toBeTruthy();
    expect(screen.getByText("Refinement cancelled")).toBeTruthy();
  });

  it("polls a rehydrated interrupted recording refinement to completion", async () => {
    vi.useFakeTimers();
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      chat_history: [
        {
          sender: "product",
          content: "Refine the recording (2 actions) into a reusable workflow.",
          turn_id: "turn-refine",
          created_at: "2026-06-10T00:00:00Z",
        },
        {
          sender: "ai",
          content: "This turn was interrupted before it could finish.",
          created_at: "2026-06-10T00:00:01Z",
          turn_outcome: {
            copilot_turn_id: "turn-refine",
            terminal_reason: "interrupted",
          },
        },
      ],
      proposed_workflow: null,
      auto_accept: false,
    };

    render(chatUi({}));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText("Workflow refinement stopped")).toBeTruthy();

    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      chat_history: [
        {
          sender: "product",
          content: "Refine the recording (2 actions) into a reusable workflow.",
          turn_id: "turn-refine",
          created_at: "2026-06-10T00:00:00Z",
        },
        {
          sender: "ai",
          content: "The recorded workflow is ready.",
          turn_id: "turn-refine",
          created_at: "2026-06-10T00:00:03Z",
          turn_outcome: {
            copilot_turn_id: "turn-refine",
            terminal_reason: "completed",
          },
          narrative_payload: {
            turnId: "turn-refine",
            proposalDisposition: "review_untested",
            draft: {
              blockCount: 2,
              blockLabels: ["Open page", "Enter search"],
              summary: "Built from the recording",
            },
            terminal: "response",
          },
        },
      ],
      proposed_workflow: null,
      auto_accept: false,
    };
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });

    expect(screen.getByText("Workflow refinement complete")).toBeTruthy();
    expect(screen.getByText("Refined into a reusable workflow")).toBeTruthy();
  });

  it("fails a rehydrated refinement when a restarted recovery poll gives up", async () => {
    vi.useFakeTimers();
    const question = {
      interaction_id: "question-1",
      turn_id: "turn-refine",
      tool_call_id: "call-1",
      status: "pending",
      response: null,
      created_at: "2026-06-10T00:00:01Z",
      resolved_at: null,
      parts: [
        {
          part_id: "format",
          prompt: "Which format?",
          choices: [{ choice_id: "csv", text: "CSV" }],
        },
      ],
    };
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      chat_history: [
        {
          sender: "product",
          content: "Refine the recording (2 actions) into a reusable workflow.",
          turn_id: "turn-refine",
          created_at: "2026-06-10T00:00:00Z",
        },
        {
          sender: "ai",
          content: "This turn was interrupted before it could finish.",
          created_at: "2026-06-10T00:00:01Z",
          turn_outcome: {
            copilot_turn_id: "turn-refine",
            terminal_reason: "interrupted",
          },
        },
      ],
      proposed_workflow: null,
      auto_accept: false,
    };
    render(chatUi({}));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(
      screen.getByText("Turning your task demonstration into a workflow"),
    ).toBeTruthy();

    Object.assign(historyResponse.data, {
      question_interactions: [question],
      pending_question_cancel_token: "stop",
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });

    cancelPost.mockResolvedValueOnce({
      data: { ...question, status: "resolved", response: { skipped: true } },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Skip" }));
    });
    historyGet.mockRejectedValue(new Error("offline"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000 + 3_000 + 5_000);
    });

    expect(screen.getByText("Workflow refinement stopped")).toBeTruthy();
    expect(screen.getByText("Refinement needs attention")).toBeTruthy();
  });

  it("shows current-turn feedback while an older refinement recovers", async () => {
    useRecordingRefinementEvidenceStore.getState().set({
      nonce: "n-refine",
      evidence: {
        schema_version: 1,
        recording: { browser_session_id: "pbs-1" },
        actions: [{ action_id: "a001" }],
        deleted_action_ids: [],
        truncated_action_count: 0,
        provenance: { source: "browser_recording" },
      },
    });

    await renderChat({
      initialAction: { kind: "refine_recording", nonce: "n-refine" },
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    vi.useFakeTimers();
    await act(async () => {
      streamCalls[0]?.onMessage({
        type: "turn_start",
        turn_id: "turn-refine",
        turn_index: 0,
        mode: "build",
        timestamp: "2026-06-10T00:00:00Z",
      });
      streamCalls[0]?.reject(new Error("stream disconnected"));
    });
    expect(
      screen.getByText("Turning your task demonstration into a workflow"),
    ).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    await submit("Add a final confirmation step");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(postStreaming).toHaveBeenCalledTimes(2);

    expect(
      screen.getByText("Copilot is working on your request…"),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "History" }).hasAttribute("disabled"),
    ).toBe(true);

    Object.assign(historyResponse.data, {
      question_interactions: [
        {
          interaction_id: "question-old",
          turn_id: "turn-refine",
          tool_call_id: "call-old",
          status: "pending",
          response: null,
          created_at: "2026-06-10T00:00:02Z",
          resolved_at: null,
          parts: [
            {
              part_id: "format",
              prompt: "Which format?",
              choices: [{ choice_id: "csv", text: "CSV" }],
            },
          ],
        },
      ],
      pending_question_cancel_token: "stop-old",
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });

    expect(
      screen.getByRole("button", { name: "History" }).hasAttribute("disabled"),
    ).toBe(true);
  });

  it("does not use another turn's proposal as recovered refinement success", async () => {
    useRecordingRefinementEvidenceStore.getState().set({
      nonce: "n-refine",
      evidence: {
        schema_version: 1,
        recording: { browser_session_id: "pbs-1" },
        actions: [{ action_id: "a001" }],
        deleted_action_ids: [],
        truncated_action_count: 0,
        provenance: { source: "browser_recording" },
      },
    });

    await renderChat({
      initialAction: { kind: "refine_recording", nonce: "n-refine" },
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    vi.useFakeTimers();
    await act(async () => {
      streamCalls[0]?.onMessage({
        type: "turn_start",
        turn_id: "turn-refine",
        turn_index: 0,
        mode: "build",
        timestamp: "2026-06-10T00:00:00Z",
      });
      streamCalls[0]?.reject(new Error("stream disconnected"));
    });

    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      chat_history: [
        {
          sender: "product",
          content: "Refine the recording (1 actions) into a reusable workflow.",
          turn_id: "turn-refine",
          created_at: "2026-06-10T00:00:00Z",
        },
        {
          sender: "ai",
          content: "I could not produce a workflow.",
          created_at: "2026-06-10T00:00:02Z",
          turn_outcome: {
            copilot_turn_id: "turn-refine",
            terminal_reason: "completed",
          },
          narrative_payload: {
            turnId: "turn-refine",
            proposalDisposition: "no_proposal",
            terminal: "response",
          },
        },
      ],
      proposed_workflow: { workflow_id: "older-draft" },
      auto_accept: false,
    };
    Object.assign(historyResponse.data, {
      proposed_workflow_metadata: { owner_turn_id: "turn-older" },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });

    expect(screen.getByText("Workflow refinement stopped")).toBeTruthy();
    expect(screen.getByText("Refinement needs attention")).toBeTruthy();
    expect(screen.queryByText("Workflow ready to review")).toBeNull();
  });

  it("does not report recording refinement complete without a workflow result", async () => {
    useRecordingRefinementEvidenceStore.getState().set({
      nonce: "n-refine",
      evidence: {
        schema_version: 1,
        recording: { browser_session_id: "pbs-1" },
        actions: [{ action_id: "a001" }],
        deleted_action_ids: [],
        truncated_action_count: 0,
        provenance: { source: "browser_recording" },
      },
    });

    await renderChat({
      initialAction: { kind: "refine_recording", nonce: "n-refine" },
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    await act(async () => {
      streamCalls[0]?.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        turn_index: 0,
        mode: "build",
        timestamp: "2026-06-10T00:00:00Z",
      });
      streamCalls[0]?.onMessage({
        type: "response",
        turn_id: "turn-1",
        message: "I could not produce a workflow.",
        workflow_copilot_chat_id: "chat-1",
        response_time: "2026-06-10T00:00:02Z",
        updated_workflow: null,
        proposal_disposition: "no_proposal",
      });
      streamCalls[0]?.resolve();
    });

    expect(screen.getByText("Workflow refinement stopped")).toBeTruthy();
    expect(screen.getByText("Refinement needs attention")).toBeTruthy();
    expect(screen.queryByText("Workflow ready to review")).toBeNull();
  });

  it("cancels recording refinement when the send aborts before the request", async () => {
    let resolveClient:
      | ((client: { postStreaming: typeof postStreaming }) => void)
      | null = null;
    getSseClient.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveClient = resolve;
        }),
    );
    cancelPost.mockRejectedValueOnce(new Error("cancel unavailable"));
    useRecordingRefinementEvidenceStore.getState().set({
      nonce: "n-refine",
      evidence: {
        schema_version: 1,
        recording: { browser_session_id: "pbs-1" },
        actions: [{ action_id: "a001" }],
        deleted_action_ids: [],
        truncated_action_count: 0,
        provenance: { source: "browser_recording" },
      },
    });

    await renderChat({
      initialAction: { kind: "refine_recording", nonce: "n-refine" },
    });
    await waitFor(() => expect(getSseClient).toHaveBeenCalledTimes(1));

    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(cancelPost).toHaveBeenCalledTimes(1));
    await act(async () => {
      resolveClient?.({ postStreaming });
    });

    await waitFor(() =>
      expect(screen.getByText("Workflow refinement stopped")).toBeTruthy(),
    );
    expect(screen.getByText("Refinement cancelled")).toBeTruthy();
    expect(postStreaming).not.toHaveBeenCalled();
  });

  it("keeps code authoring enabled for a typed diagnose action on a code workflow", async () => {
    BOOLEAN_FLAGS.WORKFLOW_COPILOT_CODE_BLOCK_MODE = true;
    BOOLEAN_FLAGS.CODE_BLOCK_ACCESS = true;
    const view = await renderChat();
    expect(
      screen.getByRole("button", { name: "Switch mode" }).textContent,
    ).toContain("Build with code");

    view.rerender(
      chatUi({
        initialAction: {
          kind: "diagnose_run",
          workflowRunId: "wr_clicked",
          nonce: "n1",
        },
      }),
    );
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(streamCalls[0]?.body.product_action).toBe("diagnose_run");
    expect(streamCalls[0]?.body.mode).toBe("build");
    expect(streamCalls[0]?.body.code_block).toBe(true);
    expect(
      screen.getByRole("button", { name: "Switch mode" }).textContent,
    ).toContain("Build with code");
  });

  it("re-fires on a new action nonce", async () => {
    const view = await renderChat({
      initialAction: {
        kind: "diagnose_run",
        workflowRunId: "wr_1",
        nonce: "n1",
      },
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]?.resolve();
    });

    view.rerender(
      chatUi({
        initialAction: {
          kind: "diagnose_run",
          workflowRunId: "wr_2",
          nonce: "n2",
        },
      }),
    );
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));

    expect(streamCalls[1]?.body.workflow_run_id).toBe("wr_2");
    expect(streamCalls[1]?.body.product_action).toBe("diagnose_run");
  });

  it("retries history and posts a product action that arrives after initial readiness failed", async () => {
    historyGet
      .mockRejectedValueOnce(new Error("workflow is not ready yet"))
      .mockRejectedValueOnce(new Error("workflow is not ready yet"));
    const view = await renderChat();
    await waitFor(() => expect(historyGet).toHaveBeenCalled());
    const callsBeforeAction = historyGet.mock.calls.length;

    view.rerender(
      chatUi({
        initialAction: {
          kind: "diagnose_run",
          workflowRunId: "wr_late",
          nonce: "n-late",
        },
      }),
    );

    await waitFor(() =>
      expect(historyGet.mock.calls.length).toBeGreaterThan(callsBeforeAction),
    );
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    expect(streamCalls[0]?.body.product_action).toBe("diagnose_run");
    expect(streamCalls[0]?.body.workflow_run_id).toBe("wr_late");
  });

  it("does not consume a refine action when history readiness keeps failing", async () => {
    vi.useFakeTimers();
    historyGet.mockRejectedValue(new Error("workflow is not ready yet"));
    const consumed = vi.fn();
    const view = render(chatUi({ onInitialMessageConsumed: consumed }));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(historyGet).toHaveBeenCalled();
    const callsBeforeAction = historyGet.mock.calls.length;

    view.rerender(
      chatUi({
        initialAction: { kind: "refine_recording", nonce: "n-refine" },
        onInitialMessageConsumed: consumed,
      }),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(historyGet.mock.calls.length).toBeGreaterThan(callsBeforeAction);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(consumed).not.toHaveBeenCalled();
    expect(toast).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: "Could not auto-send message" }),
    );
  });

  it("still posts the typed action when the live browser was not ready yet", async () => {
    const props: ChatProps = {
      initialAction: {
        kind: "diagnose_run",
        workflowRunId: "wr_queued",
        nonce: "n1",
      },
      requiresLiveBrowser: true,
      isLiveBrowserReady: false,
    };
    const view = render(chatUi(props));
    await waitFor(() =>
      expect(
        screen.getByText("Diagnose run wr_queued and repair the workflow."),
      ).toBeTruthy(),
    );
    expect(
      screen.getByText("Prompt queued. Waiting for live browser..."),
    ).toBeTruthy();
    expect(postStreaming).not.toHaveBeenCalled();

    view.rerender(
      chatUi({
        ...props,
        isLiveBrowserReady: true,
        liveBrowserSessionId: "pbs_1",
      }),
    );
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(streamCalls[0]?.body.product_action).toBe("diagnose_run");
    expect(streamCalls[0]?.body.workflow_run_id).toBe("wr_queued");
  });

  it("renders a persisted product row as an event line, not a user bubble", async () => {
    historyResponse.data = {
      workflow_copilot_chat_id: "chat_1",
      chat_history: [
        {
          sender: "product",
          content: "Diagnose run wr_1 and repair the workflow.",
          created_at: "2026-09-01T00:00:00.000Z",
        },
      ],
      proposed_workflow: null,
      auto_accept: false,
    };
    await renderChat({});

    const row = await screen.findByText(
      "Diagnose run wr_1 and repair the workflow.",
    );
    expect(row.closest('[role="status"]')).not.toBeNull();
  });

  it("falls back to the route param when no prop is given", async () => {
    routeParams.current = {
      workflowPermanentId: "wpid_1",
      workflowRunId: "wr_route",
    };
    await renderChat({});
    await submit("this run failed, fix it");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(streamCalls[0]?.body.workflow_run_id).toBe("wr_route");
  });
});
