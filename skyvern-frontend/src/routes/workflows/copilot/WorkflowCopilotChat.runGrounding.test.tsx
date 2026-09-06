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

type StreamBody = {
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

const { streamCalls, postStreaming, cancelPost, historyResponse, routeParams } =
  vi.hoisted(() => {
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
    return {
      streamCalls: calls,
      postStreaming: streaming,
      cancelPost: post,
      historyResponse: history,
      routeParams: params,
    };
  });

vi.mock("@/api/sse", () => ({
  getSseClient: vi.fn().mockResolvedValue({ postStreaming }),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn().mockResolvedValue({
    get: vi.fn().mockImplementation(() => Promise.resolve(historyResponse)),
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
  useWorkflowHasChangesStore: () => ({ getSaveData: () => saveData }),
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
  cancelPost.mockClear();
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
