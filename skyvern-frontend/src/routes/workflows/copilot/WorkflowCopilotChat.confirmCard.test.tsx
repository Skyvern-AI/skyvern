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
  mode: string | null;
  code_block: boolean | null;
};
type StreamCall = {
  body: StreamBody;
  onMessage: (payload: unknown) => boolean;
  resolve: () => void;
  reject: (error: unknown) => void;
};

const { mockCopilotUxV1Enabled } = vi.hoisted(() => ({
  mockCopilotUxV1Enabled: vi.fn(() => true),
}));

vi.mock("posthog-js/react", () => ({
  useFeatureFlagEnabled: () => mockCopilotUxV1Enabled(),
}));

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

vi.mock("@/store/WorkflowHasChangesStore", () => ({
  useWorkflowHasChangesStore: () => ({ getSaveData: () => saveData }),
}));

vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: undefined }),
}));

import { WorkflowCopilotChat } from "./WorkflowCopilotChat";

async function renderChat() {
  const view = render(
    <FeatureFlagContext.Provider value={() => false}>
      <FeatureFlagValueContext.Provider value={() => undefined}>
        <WorkflowCopilotChat />
      </FeatureFlagValueContext.Provider>
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

const CONFIRM_NOTE =
  "(Note: Diagnosing a failed run doesn't edit the workflow on its own — confirm and I'll apply the change.)";

const terminalResponse = (message: string) => ({
  type: "response" as const,
  workflow_copilot_chat_id: "chat-1",
  message,
  updated_workflow: null,
  response_time: "2026-05-25T00:00:05Z",
  proposal_disposition: "no_proposal" as const,
});

function diagnoseNarrativePayload(message: string) {
  return {
    turnId: "turn-1",
    turnIndex: 0,
    mode: "diagnose",
    designStarted: false,
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

beforeEach(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  mockCopilotUxV1Enabled.mockReset();
  mockCopilotUxV1Enabled.mockReturnValue(true);
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
});

async function sendConfirmNote() {
  await submit("fix the failed run");
  await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
  await act(async () => {
    streamCalls[0]!.onMessage({
      ...terminalResponse(CONFIRM_NOTE),
      narrative_payload: diagnoseNarrativePayload(CONFIRM_NOTE),
    });
    streamCalls[0]!.resolve();
  });
}

describe("WorkflowCopilotChat — Confirm chip (SKY-12137)", () => {
  it("renders Confirm + Tell it what to change instead when the turn asks for a typed confirmation", async () => {
    await renderChat();
    await sendConfirmNote();

    expect(screen.getByRole("button", { name: "Confirm" })).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Tell it what to change instead" }),
    ).toBeTruthy();
  });

  it("sends 'Confirmed.' as a real turn when Confirm is clicked", async () => {
    await renderChat();
    await sendConfirmNote();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    });

    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]?.body.message).toBe("Confirmed.");
  });

  it("focuses the composer instead of sending when 'Tell it what to change instead' is clicked", async () => {
    await renderChat();
    await sendConfirmNote();

    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: "Tell it what to change instead" }),
      );
    });

    expect(postStreaming).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(textarea());
  });

  it("does not render for an ordinary QA turn with no confirm-request phrasing", async () => {
    await renderChat();
    await submit("what does this workflow do?");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    const plainAnswer = "It scrapes Hacker News for the top stories.";
    await act(async () => {
      streamCalls[0]!.onMessage({
        ...terminalResponse(plainAnswer),
        narrative_payload: diagnoseNarrativePayload(plainAnswer),
      });
      streamCalls[0]!.resolve();
    });

    expect(screen.queryByRole("button", { name: "Confirm" })).toBeNull();
  });

  it("does not render when the flag is off", async () => {
    mockCopilotUxV1Enabled.mockReturnValue(false);
    await renderChat();
    await sendConfirmNote();

    expect(screen.queryByRole("button", { name: "Confirm" })).toBeNull();
  });
});
