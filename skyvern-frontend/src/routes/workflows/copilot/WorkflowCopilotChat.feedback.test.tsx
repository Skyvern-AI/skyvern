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
import { WorkflowCopilotChat } from "./WorkflowCopilotChat";

type StreamBody = { message: string };
type StreamCall = {
  body: StreamBody;
  onMessage: (payload: unknown) => boolean;
  resolve: () => void;
  reject: (error: unknown) => void;
};

const { streamCalls, postStreaming, clientPost, historyResponse } = vi.hoisted(
  () => {
    const calls: StreamCall[] = [];
    const post = vi.fn().mockResolvedValue({
      data: {
        workflow_copilot_chat_message_id: "msg-ai-1",
        feedback: {
          rating: "down",
          reason: null,
          rated_at: "2026-09-22T00:00:00Z",
        },
      },
    });
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
      clientPost: post,
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
    post: clientPost,
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

vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: undefined }),
}));

function narrativePayload(message: string) {
  return {
    turnId: "turn-1",
    turnIndex: 0,
    mode: "build",
    designStarted: true,
    designEnded: true,
    draft: null,
    blocks: [],
    terminal: "response" as const,
    terminalMessage: message,
    narrativeSummary: message,
    priorBlockCount: null,
    designActivity: [],
    startedAt: null,
    endedAt: null,
  };
}

async function renderChat() {
  const view = render(
    <FeatureFlagContext.Provider value={() => false}>
      <WorkflowCopilotChat />
    </FeatureFlagContext.Provider>,
  );
  await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
  return view;
}

beforeEach(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  streamCalls.length = 0;
  postStreaming.mockClear();
  clientPost.mockClear();
  historyResponse.data = {
    workflow_copilot_chat_id: null,
    chat_history: [],
    proposed_workflow: null,
    auto_accept: false,
  };
});

afterEach(() => {
  cleanup();
});

describe("WorkflowCopilotChat — turn feedback", () => {
  it("shows thumbs under a live terminal narrative turn and rates it by turn id", async () => {
    await renderChat();
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "build me a workflow" } });
    await act(async () => {
      fireEvent.keyDown(textarea, { key: "Enter" });
    });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("feedback-thumbs")).toBeNull();

    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "response",
        workflow_copilot_chat_id: "chat-1",
        message: "Built three blocks.",
        updated_workflow: null,
        response_time: "2026-09-22T00:00:05Z",
        proposal_disposition: "no_proposal",
        narrative_payload: narrativePayload("Built three blocks."),
      });
      streamCalls[0]!.resolve();
    });

    const thumbs = await screen.findAllByTestId("feedback-thumbs");
    expect(thumbs).toHaveLength(1);

    fireEvent.click(screen.getByLabelText("Thumbs down"));
    await waitFor(() =>
      expect(clientPost).toHaveBeenCalledWith(
        "/workflow/copilot/message-feedback",
        expect.objectContaining({
          workflow_copilot_chat_id: "chat-1",
          turn_id: "turn-1",
          rating: "down",
        }),
      ),
    );
    expect(await screen.findByLabelText("Feedback reason")).toBeTruthy();
  });

  it("hydrates a saved rating from chat history and never rates a user row", async () => {
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      chat_history: [
        {
          workflow_copilot_chat_message_id: "msg-user-0",
          sender: "user",
          content: "hello",
          created_at: "2026-09-21T00:00:00Z",
        },
        {
          workflow_copilot_chat_message_id: "msg-ai-0",
          sender: "ai",
          content: "Hi. What should this workflow do?",
          turn_id: "turn-0",
          created_at: "2026-09-21T00:00:05Z",
          narrative_payload: narrativePayload(
            "Hi. What should this workflow do?",
          ),
          feedback: {
            rating: "down",
            reason: "It skipped the second page",
            rated_at: "2026-09-21T00:01:00Z",
          },
        },
        {
          workflow_copilot_chat_message_id: "msg-user-1",
          sender: "user",
          content: "build me a workflow",
          created_at: "2026-09-22T00:00:00Z",
        },
        {
          workflow_copilot_chat_message_id: "msg-ai-1",
          sender: "ai",
          content: "Built three blocks.",
          turn_id: "turn-1",
          created_at: "2026-09-22T00:00:05Z",
          narrative_payload: narrativePayload("Built three blocks."),
        },
      ],
      proposed_workflow: null,
      auto_accept: false,
    };
    await renderChat();

    const thumbs = await screen.findAllByTestId("feedback-thumbs");
    expect(thumbs).toHaveLength(2);
    // Only the latest turn keeps its thumbs visible; the rated older turn hides them until hover.
    expect(thumbs[0]!.getAttribute("data-subtle")).toBe("true");
    expect(thumbs[1]!.getAttribute("data-subtle")).toBeNull();
    expect(
      screen
        .getAllByLabelText("Thumbs down")
        .map((button) => button.getAttribute("aria-pressed")),
    ).toEqual(["true", "false"]);
    expect(screen.queryByText("Thanks, that helps.")).toBeNull();
    expect(screen.queryByLabelText("Feedback reason")).toBeNull();
  });
});
