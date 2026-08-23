import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  FeatureFlagContext,
  FeatureFlagValueContext,
} from "@/hooks/useFeatureFlag";

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

const BOOLEAN_FLAGS: Record<string, boolean> = {
  ENABLE_WORKFLOW_COPILOT_V2: true,
  WORKFLOW_COPILOT_CODE_BLOCK_MODE: false,
  CODE_BLOCK_ACCESS: false,
};

type ChatProps = {
  workflowRunId?: string | null;
};

function chatUi(props: ChatProps) {
  return (
    <FeatureFlagContext.Provider value={(name) => BOOLEAN_FLAGS[name]}>
      <FeatureFlagValueContext.Provider value={() => undefined}>
        <WorkflowCopilotChat {...props} />
      </FeatureFlagValueContext.Provider>
    </FeatureFlagContext.Provider>
  );
}

async function renderChat(props: ChatProps = {}) {
  const view = render(chatUi(props));
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
