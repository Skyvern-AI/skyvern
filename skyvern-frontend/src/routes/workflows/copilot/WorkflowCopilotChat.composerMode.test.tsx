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
  message: string;
  mode: string | null;
  code_block: boolean | null;
  selected_block_label: string | null;
};
type StreamCall = {
  body: StreamBody;
  onMessage: (payload: unknown) => boolean;
  resolve: () => void;
  reject: (error: unknown) => void;
};

const { streamCalls, postStreaming, cancelPost, historyResponse } = vi.hoisted(
  () => {
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
    return {
      streamCalls: calls,
      postStreaming: streaming,
      cancelPost: post,
      historyResponse: history,
    };
  },
);

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
    useParams: () => ({
      workflowPermanentId: "wpid_1",
      workflowRunId: undefined,
    }),
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

vi.mock("@/store/WorkflowHasChangesStore", () => {
  const state = { getSaveData: () => saveData, setSaveBlockedReason: () => {} };
  return {
    useWorkflowHasChangesStore: Object.assign(() => state, {
      getState: () => state,
    }),
  };
});

// Unrelated to this file's tests; the real hook needs a QueryClientProvider
// this harness doesn't set up.
vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: undefined }),
}));

import {
  WorkflowCopilotChat,
  canonicalRecoveriesByWorkflow,
} from "./WorkflowCopilotChat";

type FlagConfig = {
  codeBlockMode?: boolean;
};

async function renderChat(flags: FlagConfig) {
  const booleanFlags: Record<string, boolean> = {
    WORKFLOW_COPILOT_CODE_BLOCK_MODE: flags.codeBlockMode ?? false,
    CODE_BLOCK_ACCESS: flags.codeBlockMode ?? false,
  };
  const view = render(
    <FeatureFlagContext.Provider value={(name) => booleanFlags[name]}>
      <WorkflowCopilotChat />
    </FeatureFlagContext.Provider>,
  );
  await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
  return view;
}

function textarea(): HTMLTextAreaElement {
  return screen.getByRole("textbox") as HTMLTextAreaElement;
}

async function submit(value: string) {
  fireEvent.change(textarea(), { target: { value } });
  await act(async () => {
    fireEvent.keyDown(textarea(), { key: "Enter" });
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
});

afterEach(() => {
  cleanup();
  canonicalRecoveriesByWorkflow.clear();
});

describe("WorkflowCopilotChat — canvas selection context", () => {
  afterEach(() => {
    window.history.pushState(null, "", "/");
  });

  it("sends the canvas selection as selected_block_label", async () => {
    window.history.pushState(null, "", "/?selected-block=login");
    await renderChat({});
    await submit("why is this block failing?");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(streamCalls[0]?.body.selected_block_label).toBe("login");
  });

  it("sends null without a selection", async () => {
    await renderChat({});
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(streamCalls[0]?.body.selected_block_label).toBe(null);
  });
});
