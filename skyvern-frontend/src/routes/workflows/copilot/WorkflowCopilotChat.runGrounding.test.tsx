import {
  canonicalRecoveriesByWorkflow,
  WorkflowCopilotChat,
} from "./WorkflowCopilotChat";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FeatureFlagContext } from "@/hooks/useFeatureFlag";
import {
  beginYamlCommit,
  createYamlCommitOwner,
  finishYamlCommit,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";

type StreamBody = {
  cancel_token?: string | null;
  code_block?: boolean | null;
  message: string;
  mode?: string | null;
  workflow_run_id?: string | null;
  product_action?: string | null;
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

const { posthogCapture } = vi.hoisted(() => ({ posthogCapture: vi.fn() }));
vi.mock("posthog-js", () => ({ default: { capture: posthogCapture } }));

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
    { getState: () => ({ hasChanges: false, setSaveBlockedReason: () => {} }) },
  ),
}));

// Unrelated to this file's tests; the real hook needs a QueryClientProvider
// this harness doesn't set up.
vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQueries: (ids: readonly string[]) =>
    ids.map(() => ({ data: undefined })),
  useWorkflowRunQuery: () => ({ data: undefined }),
}));

import type { CopilotProductAction } from "./workflowCopilotTypes";

const BOOLEAN_FLAGS: Record<string, boolean> = {
  WORKFLOW_COPILOT_CODE_BLOCK_MODE: false,
  CODE_BLOCK_ACCESS: false,
};

type ChatProps = {
  onWorkflowUpdate?: NonNullable<
    ComponentProps<typeof WorkflowCopilotChat>
  >["onWorkflowUpdate"];
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
  sessionStorage.clear();
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
  posthogCapture.mockClear();
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
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
  canonicalRecoveriesByWorkflow.clear();
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

  it("recovers the turn when the turn_start frame is lost", async () => {
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: saveData.workflow }
          : historyResponse,
      ),
    );
    await renderChat();
    await submit("Add a final confirmation step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    vi.useFakeTimers();
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      request_turn_id: null,
      chat_history: [
        {
          sender: "user",
          content: "Add a final confirmation step",
          turn_id: "turn-earlier-same-receipt",
          created_at: new Date().toISOString(),
        },
      ],
      proposed_workflow: null,
      auto_accept: false,
    };

    await act(async () => {
      streamCalls[0]?.reject(new Error("stream disconnected"));
      await vi.advanceTimersByTimeAsync(2_000);
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
      screen.getByText(
        "The connection dropped, so Copilot is checking whether this turn finished.",
      ),
    ).toBeTruthy();

    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      request_turn_id: "turn-lost-start",
      chat_history: [
        {
          sender: "user",
          content: "Add a final confirmation step",
          turn_id: "turn-lost-start",
          created_at: new Date().toISOString(),
        },
      ],
      proposed_workflow: null,
      auto_accept: false,
    };
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(
      historyGet.mock.calls.filter(
        ([, config]) =>
          config?.params?.request_cancel_token ===
          streamCalls[0]?.body.cancel_token,
      ),
    ).toHaveLength(2);

    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      request_turn_id: "turn-lost-start",
      chat_history: [
        historyResponse.data.chat_history[0]!,
        {
          sender: "ai",
          content: "The workflow is ready.",
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
      await vi.advanceTimersByTimeAsync(5_000);
    });

    expect(screen.getByText("The workflow is ready.")).toBeTruthy();
  });

  it.each([false, true])(
    "releases the original reservation when a delayed turn finishes (canonical advanced: %s)",
    async (advanced) => {
      const apply = vi.fn();
      let canonical = saveData.workflow;
      historyGet.mockImplementation((path: string) =>
        Promise.resolve(
          path === "/workflows/wpid_1" ? { data: canonical } : historyResponse,
        ),
      );
      await renderChat({ onWorkflowUpdate: apply });
      await submit("Add a final confirmation step");
      await waitFor(() => expect(streamCalls).toHaveLength(1));
      vi.useFakeTimers();
      historyResponse.data.request_turn_id = null;
      await act(async () => {
        streamCalls[0]!.reject(
          new Error("connection dropped before turn_start"),
        );
        await vi.advanceTimersByTimeAsync(0);
      });
      const reservation =
        useWorkflowYamlEditorStore.getState().copilotAcceptance;
      expect(reservation).not.toBeNull();
      const owner = createYamlCommitOwner("wpid_1");
      expect(beginYamlCommit(owner)).toBe(false);

      historyResponse.data.workflow_copilot_chat_id = "chat-delayed";
      historyResponse.data.request_turn_id = "turn-delayed";
      historyResponse.data.chat_history = [
        {
          sender: "user",
          content: "Add a final confirmation step",
          turn_id: "turn-delayed",
          created_at: new Date().toISOString(),
        },
      ];
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
        reservation,
      );

      expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
      expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Reject" }));
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/cancel",
        {
          cancel_token: streamCalls[0]!.body.cancel_token,
          workflow_copilot_chat_id: "chat-delayed",
          source: "stop_button",
        },
        expect.anything(),
      );
      expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
        reservation,
      );

      const historyReads: Array<(response: typeof historyResponse) => void> =
        [];
      historyGet.mockImplementation((path: string) =>
        path === "/workflows/wpid_1"
          ? Promise.resolve({ data: canonical })
          : new Promise<typeof historyResponse>((resolve) =>
              historyReads.push(resolve),
            ),
      );
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Retry" }));
        await vi.advanceTimersByTimeAsync(2_000);
      });
      expect(historyReads).toHaveLength(1);
      if (advanced)
        canonical = { ...saveData.workflow, workflow_id: "wf_committed" };
      historyResponse.data.chat_history.push({
        sender: "ai",
        content: "The workflow is ready.",
        created_at: new Date().toISOString(),
        turn_outcome: {
          copilot_turn_id: "turn-delayed",
          terminal_reason: "completed",
        },
        narrative_payload: {
          turnId: "turn-delayed",
          proposalDisposition: "review_untested",
          draft: { blockCount: 1, blockLabels: ["Open page"] },
          terminal: "response",
        },
      });
      historyGet.mockClear();
      await act(async () => historyReads[0]!(historyResponse));

      expect(screen.getByText("The workflow is ready.")).toBeTruthy();
      expect(historyGet).toHaveBeenCalledWith(
        "/workflows/wpid_1",
        expect.anything(),
      );
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
      expect(beginYamlCommit(owner)).toBe(true);
      finishYamlCommit(owner);
      if (advanced) {
        expect(apply).toHaveBeenCalledWith(
          canonical,
          expect.objectContaining({ persisted: true, applied: true }),
        );
      } else {
        expect(apply).not.toHaveBeenCalled();
      }
      await submit("Continue with another edit");
      expect(streamCalls).toHaveLength(2);
    },
  );

  it("scopes lost-turn recovery to the chat that sent the request", async () => {
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-existing",
      chat_history: [],
      proposed_workflow: null,
      auto_accept: false,
    };
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: saveData.workflow }
          : historyResponse,
      ),
    );
    await renderChat();
    await submit("Add a final confirmation step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    historyGet.mockClear();
    vi.useFakeTimers();
    historyResponse.data.chat_history = [
      {
        sender: "user",
        content: "Add a final confirmation step",
        turn_id: "turn-lost-start",
        created_at: new Date().toISOString(),
      },
    ];

    await act(async () => {
      streamCalls[0]?.reject(new Error("stream disconnected"));
      await vi.advanceTimersByTimeAsync(2_000);
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

  it("requires reconciliation for a null-ID recovery after its poll expires", async () => {
    const apply = vi.fn();
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: saveData.workflow }
          : historyResponse,
      ),
    );
    await renderChat({ onWorkflowUpdate: apply });
    vi.useFakeTimers();
    await submit("Add a final confirmation step");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
      streamCalls[0]!.reject(new Error("connection dropped before turn_start"));
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(1_499_999));
    expect(
      screen.getByText(/Copilot is checking whether this turn finished/),
    ).toBeTruthy();

    await act(async () => vi.advanceTimersByTimeAsync(1));

    expect(
      screen.queryByText(/Copilot is checking whether this turn finished/),
    ).toBeNull();
    expect(
      screen.getByText(/Could not confirm whether Copilot saved changes/),
    ).toBeTruthy();
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    expect(apply).not.toHaveBeenCalled();
  });

  it("requires reconciliation for a late-adopted recovery after its poll expires", async () => {
    const apply = vi.fn();
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: saveData.workflow }
          : historyResponse,
      ),
    );
    await renderChat({ onWorkflowUpdate: apply });
    await submit("Add a final confirmation step");
    await waitFor(() => expect(streamCalls).toHaveLength(1));
    vi.useFakeTimers();
    historyResponse.data.request_turn_id = null;
    await act(async () => {
      streamCalls[0]!.reject(new Error("connection dropped before turn_start"));
      await vi.advanceTimersByTimeAsync(0);
    });
    const reservation = useWorkflowYamlEditorStore.getState().copilotAcceptance;
    expect(reservation).not.toBeNull();

    historyResponse.data.workflow_copilot_chat_id = "chat-delayed";
    historyResponse.data.request_turn_id = "turn-delayed";
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
      reservation,
    );

    await act(async () => vi.advanceTimersByTimeAsync(1_497_999));
    expect(
      screen.getByText(/Copilot is checking whether this turn finished/),
    ).toBeTruthy();

    await act(async () => vi.advanceTimersByTimeAsync(1));

    expect(
      screen.queryByText(/Copilot is checking whether this turn finished/),
    ).toBeNull();
    expect(
      screen.getByText(/Could not confirm whether Copilot saved changes/),
    ).toBeTruthy();
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    expect(apply).not.toHaveBeenCalled();
  });

  it("keeps the next send blocked until reconciliation and preserves its recovery notice", async () => {
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
    await act(async () => vi.advanceTimersByTimeAsync(1_500_000));
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    await submit("Second request");
    expect(streamCalls).toHaveLength(1);
    historyResponse.data.chat_history = [
      {
        sender: "ai",
        content: "First request finished",
        created_at: new Date().toISOString(),
        turn_outcome: {
          copilot_turn_id: "turn-1",
          terminal_reason: "completed",
        },
      },
    ];
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
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

    expect(streamCalls).toHaveLength(2);
    await act(async () => vi.advanceTimersByTimeAsync(2_000));

    expect(
      screen.getAllByText(
        "The connection dropped, so Copilot is checking whether this turn finished.",
      ),
    ).toHaveLength(1);
    expect(screen.getAllByText("First request finished")).toHaveLength(1);
  });

  it("re-fires on a new action nonce", async () => {
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: saveData.workflow }
          : historyResponse,
      ),
    );
    const view = await renderChat({
      initialAction: {
        kind: "diagnose_run",
        workflowRunId: "wr_1",
        nonce: "n1",
      },
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    vi.useFakeTimers();
    const reservation = useWorkflowYamlEditorStore.getState().copilotAcceptance;
    expect(reservation).not.toBeNull();
    await act(async () => {
      streamCalls[0]?.resolve();
    });
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
      reservation,
    );
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Stopping…" })).toBeNull();
    const send = screen.getByRole("button", { name: "Send" });
    expect(send.hasAttribute("disabled")).toBe(false);
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "Diagnose another run" },
    });
    await act(async () => fireEvent.click(send));
    expect(postStreaming).toHaveBeenCalledTimes(1);
    expect(cancelPost).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalledWith({
      title: "Wait for the Copilot change to finish",
      variant: "destructive",
    });
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(
      "Diagnose another run",
    );
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      request_turn_id: "turn-first",
      chat_history: [
        {
          sender: "ai",
          content: "Diagnosis complete.",
          created_at: new Date().toISOString(),
          turn_outcome: {
            copilot_turn_id: "turn-first",
            terminal_reason: "completed",
          },
        },
      ],
      proposed_workflow: null,
      auto_accept: false,
    };
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    expect(historyGet).toHaveBeenCalledWith(
      "/workflow/copilot/chat-history",
      expect.objectContaining({
        params: expect.objectContaining({
          request_cancel_token: streamCalls[0]!.body.cancel_token,
        }),
      }),
    );
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    vi.useRealTimers();

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
        screen.getByTestId("copilot-queued-message").textContent,
      ).toContain("Diagnose run wr_queued and repair the workflow."),
    );
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

  it("replaces a null-ID recovery notice when the reserved poll expires", async () => {
    const apply = vi.fn();
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: saveData.workflow }
          : historyResponse,
      ),
    );
    await renderChat({ onWorkflowUpdate: apply });
    vi.useFakeTimers();
    await submit("Add a final confirmation step");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
      streamCalls[0]!.reject(new Error("connection dropped before turn_start"));
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(1_499_999));
    expect(
      screen.getByText(/Copilot is checking whether this turn finished/),
    ).toBeTruthy();

    await act(async () => vi.advanceTimersByTimeAsync(1));

    expect(
      screen.queryByText(/Copilot is checking whether this turn finished/),
    ).toBeNull();
    expect(
      screen.getByText(/Could not confirm whether Copilot saved changes/),
    ).toBeTruthy();
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(apply).not.toHaveBeenCalled();
  });

  it("replaces a late-adopted recovery notice when the reserved poll expires", async () => {
    const apply = vi.fn();
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: saveData.workflow }
          : historyResponse,
      ),
    );
    await renderChat({ onWorkflowUpdate: apply });
    await submit("Add a final confirmation step");
    await waitFor(() => expect(streamCalls).toHaveLength(1));
    vi.useFakeTimers();
    historyResponse.data.request_turn_id = null;
    await act(async () => {
      streamCalls[0]!.reject(new Error("connection dropped before turn_start"));
      await vi.advanceTimersByTimeAsync(0);
    });
    const reservation = useWorkflowYamlEditorStore.getState().copilotAcceptance;
    expect(reservation).not.toBeNull();

    historyResponse.data.workflow_copilot_chat_id = "chat-delayed";
    historyResponse.data.request_turn_id = "turn-delayed";
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
      reservation,
    );

    await act(async () => vi.advanceTimersByTimeAsync(1_497_999));
    expect(
      screen.getByText(/Copilot is checking whether this turn finished/),
    ).toBeTruthy();

    await act(async () => vi.advanceTimersByTimeAsync(1));

    expect(
      screen.queryByText(/Copilot is checking whether this turn finished/),
    ).toBeNull();
    expect(
      screen.getByText(/Could not confirm whether Copilot saved changes/),
    ).toBeTruthy();
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(apply).not.toHaveBeenCalled();
  });

  it("keeps an earlier failed notice while a later recovery remains pending", async () => {
    await renderChat();
    vi.useFakeTimers();
    await submit("First request");
    await act(async () => {
      streamCalls[0]!.reject(
        Object.assign(
          new Error("Sorry, I encountered an error. Please try again."),
          { status: 422 },
        ),
      );
      await vi.advanceTimersByTimeAsync(1_500_000);
    });
    expect(
      screen.getAllByText("Sorry, I encountered an error. Please try again."),
    ).toHaveLength(1);

    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: saveData.workflow }
          : historyResponse,
      ),
    );
    historyResponse.data.request_turn_id = null;
    historyResponse.data.chat_history = [];
    await submit("Second request");
    expect(streamCalls).toHaveLength(2);
    await act(async () => {
      streamCalls[1]!.reject(new Error("stream disconnected"));
      await vi.advanceTimersByTimeAsync(10_000);
    });

    expect(streamCalls).toHaveLength(2);
    await act(async () => vi.advanceTimersByTimeAsync(2_000));

    expect(
      screen.getAllByText(
        "The connection dropped, so Copilot is checking whether this turn finished.",
      ),
    ).toHaveLength(1);
    expect(
      screen.getAllByText("Sorry, I encountered an error. Please try again."),
    ).toHaveLength(1);
  });
});
