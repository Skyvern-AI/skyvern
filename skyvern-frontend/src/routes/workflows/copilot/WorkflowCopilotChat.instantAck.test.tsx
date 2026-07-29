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
  fix_origin?: boolean;
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
  workflowRunQueryMock,
  mockFeatureFlagEnabled,
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
  return {
    streamCalls: calls,
    postStreaming: streaming,
    cancelPost: post,
    historyResponse: history,
    routeParams: params,
    workflowRunQueryMock: vi.fn(),
    mockFeatureFlagEnabled: vi.fn().mockReturnValue(true),
  };
});

vi.mock("posthog-js/react", () => ({
  useFeatureFlagEnabled: mockFeatureFlagEnabled,
}));

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
  };
});

vi.mock("../hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: (options?: { workflowRunId?: string }) =>
    workflowRunQueryMock(options),
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

vi.mock("./WorkflowCopilotHistory", () => ({
  WorkflowCopilotHistory: ({
    onSelect,
  }: {
    onSelect: (chat: { workflow_copilot_chat_id: string }) => void;
  }) => (
    <button
      onClick={() => onSelect({ workflow_copilot_chat_id: "chat_other" })}
    >
      mock-select-history-chat
    </button>
  ),
}));

import { COPILOT_ACK_LINES } from "./NarrativeView";
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

function expectNoAckLines() {
  for (const line of COPILOT_ACK_LINES) {
    expect(screen.queryByText(line)).toBeNull();
  }
}

// The placeholder opens on a random line, so assert *some* ack line shows.
function expectSomeAckLine() {
  const present = COPILOT_ACK_LINES.some(
    (line) => screen.queryByText(line) !== null,
  );
  expect(present).toBe(true);
}

async function completeStream(index: number, message: string) {
  await act(async () => {
    streamCalls[index]!.onMessage({
      type: "response",
      message,
      workflow_copilot_chat_id: "chat_1",
      response_time: "2026-06-10T00:00:02Z",
    });
    streamCalls[index]!.resolve();
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
  workflowRunQueryMock.mockReset();
  workflowRunQueryMock.mockReturnValue({ data: undefined });
  mockFeatureFlagEnabled.mockReturnValue(true);
});

afterEach(() => {
  cleanup();
});

describe("WorkflowCopilotChat — instant acknowledgement", () => {
  it("REGRESSION: placeholder appears synchronously on send", async () => {
    await renderChat();
    await submit("build a workflow");

    expectSomeAckLine();
    expect(screen.getByRole("status")).toBeTruthy();
  });

  it("REGRESSION: disappears on first frame with a clean handoff", async () => {
    await renderChat();
    await submit("build a workflow");
    expectSomeAckLine();

    act(() => {
      streamCalls[0]!.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        turn_index: 0,
        mode: "build",
        timestamp: "2026-06-10T00:00:00Z",
      });
    });

    await waitFor(() => expectNoAckLines());
    expect(screen.getAllByRole("status")).toHaveLength(1);
  });

  it("REGRESSION: never renders when copilot_ux_v1 is off", async () => {
    mockFeatureFlagEnabled.mockReturnValue(false);
    await renderChat();
    await submit("build a workflow");

    expectNoAckLines();
  });

  it("REGRESSION: an Ask reply clears the placeholder when the turn completes", async () => {
    await renderChat();
    await submit("what does this workflow do?");
    expectSomeAckLine();

    // Ask turns emit no narrative frames; the placeholder clears when the turn
    // completes (isLoading falls) as the plain reply lands.
    await completeStream(0, "It scrapes headlines.");

    expect(screen.getByText("It scrapes headlines.")).toBeTruthy();
    expectNoAckLines();
  });

  it("REGRESSION: reappears on a follow-up send after a completed turn (pins the narrative reset)", async () => {
    await renderChat();

    // First turn: send, first frame, terminal response — leaves narrative.turnId
    // non-null with a terminal set, which without the send-time reset is
    // indistinguishable from a fresh send.
    await submit("build a workflow");
    expectSomeAckLine();
    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "turn_start",
        turn_id: "turn-1",
        turn_index: 0,
        mode: "build",
        timestamp: "2026-06-10T00:00:00Z",
      });
      streamCalls[0]!.onMessage({
        type: "response",
        message: "Draft ready.",
        workflow_copilot_chat_id: "chat_1",
        response_time: "2026-06-10T00:00:02Z",
        turn_id: "turn-1",
      });
      streamCalls[0]!.resolve();
    });
    await waitFor(() => expectNoAckLines());

    // Follow-up send must show the placeholder again — this fails if the
    // send-time setNarrative(EMPTY_NARRATIVE) reset is removed.
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "add a filter step" },
    });
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));

    expectSomeAckLine();
  });

  it("REGRESSION: a queued follow-up still shows the placeholder when it drains", async () => {
    await renderChat();

    // Turn 1 in flight; a follow-up sent now is queued behind it (not a 2nd
    // stream) and its user bubble is appended before turn 1's reply.
    await submit("build a workflow");
    await submit("add a filter step");
    expect(postStreaming).toHaveBeenCalledTimes(1);

    // Turn 1 completes: its AI reply lands AFTER the queued user bubble, so the
    // queued bubble is no longer the last real message. The queued prompt then
    // drains into a 2nd stream.
    await completeStream(0, "Draft ready.");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));

    // The drained send must still show the placeholder — this fails if the gate
    // requires the last real message to be the user's.
    expectSomeAckLine();
  });
});
