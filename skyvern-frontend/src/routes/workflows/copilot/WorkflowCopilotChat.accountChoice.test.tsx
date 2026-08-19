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

type StreamBody = { message: string; idempotency_key?: string | null };
type StreamCall = {
  body: StreamBody;
  onMessage: (payload: unknown) => boolean;
  resolve: () => void;
};

const { streamCalls, postStreaming, historyResponse } = vi.hoisted(() => {
  const calls: StreamCall[] = [];
  const streaming = vi.fn(
    (
      _path: string,
      body: StreamBody,
      onMessage: (payload: unknown) => boolean,
    ) =>
      new Promise<void>((resolve) => {
        calls.push({ body, onMessage, resolve });
      }),
  );
  return {
    streamCalls: calls,
    postStreaming: streaming,
    historyResponse: {
      data: {
        workflow_copilot_chat_id: null as string | null,
        chat_history: [] as unknown[],
        proposed_workflow: null,
        auto_accept: false,
      },
    },
  };
});

vi.mock("posthog-js/react", () => ({ useFeatureFlagEnabled: () => true }));
vi.mock("@/api/sse", () => ({
  getSseClient: vi.fn().mockResolvedValue({ postStreaming }),
}));
vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn().mockResolvedValue({
    get: vi.fn().mockImplementation(() => Promise.resolve(historyResponse)),
    post: vi.fn().mockResolvedValue({}),
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
    useParams: () => ({ workflowPermanentId: "wpid_1" }),
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

vi.mock("@/store/WorkflowHasChangesStore", () => ({
  useWorkflowHasChangesStore: () => ({ getSaveData: () => saveData }),
}));
vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: undefined }),
}));

import { WorkflowCopilotChat } from "./WorkflowCopilotChat";

const choices = [
  {
    connection_id: "goac_2",
    name: "Google Sheets",
    state: "error",
    email_address: "second@example.test",
  },
  {
    connection_id: "goac_1",
    name: "Google Sheets",
    state: "active",
    email_address: null,
  },
];

function narrativePayload(includeChoices = true) {
  return {
    turnId: "turn-choice",
    turnIndex: 0,
    responseType: "ASK_QUESTION",
    designStarted: true,
    designEnded: true,
    draft: null,
    blocks: [],
    terminal: "response",
    terminalMessage: "Which account should I use?",
    narrativeSummary: "Which account should I use?",
    priorBlockCount: 0,
    designActivity: [],
    startedAt: "2026-08-15T00:00:00Z",
    endedAt: "2026-08-15T00:00:01Z",
    ...(includeChoices ? { connectedAccountChoices: choices } : {}),
  };
}

async function renderChat() {
  render(
    <FeatureFlagContext.Provider value={() => false}>
      <FeatureFlagValueContext.Provider value={() => undefined}>
        <WorkflowCopilotChat />
      </FeatureFlagValueContext.Provider>
    </FeatureFlagContext.Provider>,
  );
  await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
}

async function submit(message: string) {
  const textbox = screen.getByRole("textbox");
  fireEvent.change(textbox, { target: { value: message } });
  await act(async () => fireEvent.keyDown(textbox, { key: "Enter" }));
}

async function finishChoiceAsk() {
  await submit("build a Sheets workflow");
  await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
  await act(async () => {
    streamCalls[0]!.onMessage({
      type: "response",
      workflow_copilot_chat_id: "chat-1",
      message: "Which account should I use?",
      updated_workflow: null,
      response_time: "2026-08-15T00:00:01Z",
      proposal_disposition: "no_proposal",
      narrative_payload: narrativePayload(),
    });
    streamCalls[0]!.resolve();
  });
}

beforeEach(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  streamCalls.length = 0;
  postStreaming.mockClear();
  historyResponse.data = {
    workflow_copilot_chat_id: null,
    chat_history: [],
    proposed_workflow: null,
    auto_accept: false,
  };
});

afterEach(cleanup);

describe("WorkflowCopilotChat connected account choices", () => {
  it("renders canonical rows and sends one exact active id despite a same-tick double click", async () => {
    await renderChat();
    await finishChoiceAsk();

    expect(screen.getByText("second@example.test")).toBeTruthy();
    expect(screen.getByText("Connection …goac_1")).toBeTruthy();
    const row = screen.getByRole<HTMLButtonElement>("button", {
      name: /Connection …goac_1/,
    });
    await act(async () => {
      fireEvent.click(row);
      fireEvent.click(row);
    });

    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]?.body.message).toBe("goac_1");
    expect(streamCalls[1]?.body.idempotency_key).toMatch(
      /^connected-account:turn-choice:goac_1:[0-9a-f-]{36}$/,
    );
    expect(screen.queryByText("goac_1")).toBeNull();
    expect(
      screen.getByText("Selected Google Sheets — Connection …goac_1"),
    ).toBeTruthy();
    expect(screen.getByText("Selected")).toBeTruthy();
  });

  it("does not latch the picker when attempt-key creation throws", async () => {
    await renderChat();
    await finishChoiceAsk();

    const randomUuid = vi
      .spyOn(crypto, "randomUUID")
      .mockImplementationOnce(() => {
        throw new Error("randomUUID unavailable");
      })
      .mockReturnValueOnce("00000000-0000-4000-8000-000000000001");
    const row = screen.getByRole<HTMLButtonElement>("button", {
      name: /Connection …goac_1/,
    });

    const preventExpectedError = (event: ErrorEvent) => event.preventDefault();
    window.addEventListener("error", preventExpectedError);
    fireEvent.click(row);
    window.removeEventListener("error", preventExpectedError);
    fireEvent.click(row);

    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]?.body.idempotency_key).toBe(
      "connected-account:turn-choice:goac_1:00000000-0000-4000-8000-000000000001",
    );
    randomUuid.mockRestore();
  });

  it("restores the picker after a transient selection send failure", async () => {
    await renderChat();
    await finishChoiceAsk();
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      proposed_workflow: null,
      auto_accept: false,
      chat_history: [
        {
          sender: "ai",
          content: "Which account should I use?",
          created_at: "2026-08-15T00:00:01Z",
          narrative_payload: narrativePayload(),
          turn_outcome: {
            response_kind: "clarify",
            connected_account_choices: choices,
          },
        },
      ],
    };
    postStreaming.mockRejectedValueOnce(new Error("network dropped"));

    fireEvent.click(screen.getByRole("button", { name: /Connection …goac_1/ }));

    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(
        screen.getByRole<HTMLButtonElement>("button", {
          name: /Connection …goac_1/,
        }).disabled,
      ).toBe(false),
    );
    expect(screen.queryByText("goac_1")).toBeNull();
  });

  it("uses a fresh attempt key when a choice is retried", async () => {
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      proposed_workflow: null,
      auto_accept: false,
      chat_history: [
        {
          sender: "ai",
          content: "The turn was interrupted. Please try again.",
          created_at: "2026-08-15T00:00:03Z",
          narrative_payload: narrativePayload(false),
          turn_outcome: {
            response_kind: "recover",
            connected_account_choices: choices,
          },
        },
      ],
    };

    await renderChat();
    const row = screen.getByRole<HTMLButtonElement>("button", {
      name: /Connection …goac_1/,
    });
    fireEvent.click(row);
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    const firstKey = streamCalls[0]?.body.idempotency_key;
    await act(async () => streamCalls[0]!.resolve());

    cleanup();
    streamCalls.length = 0;
    postStreaming.mockClear();
    await renderChat();

    fireEvent.click(screen.getByRole("button", { name: /Connection …goac_1/ }));
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    const secondKey = streamCalls[0]?.body.idempotency_key;

    expect(firstKey).toMatch(
      /^connected-account:turn-choice:goac_1:[0-9a-f-]{36}$/,
    );
    expect(secondKey).toMatch(
      /^connected-account:turn-choice:goac_1:[0-9a-f-]{36}$/,
    );
    expect(secondKey).not.toBe(firstKey);
    await act(async () => streamCalls[0]!.resolve());
  });

  it("routes an inactive account to reconnect instead of sending it as a choice", async () => {
    await renderChat();
    await finishChoiceAsk();

    const reconnect = screen.getByRole<HTMLAnchorElement>("link", {
      name: /second@example.test.*Reconnect/i,
    });
    expect(reconnect.getAttribute("href")).toBe("/integrations");
    fireEvent.click(reconnect);

    expect(postStreaming).toHaveBeenCalledTimes(1);
  });

  it("hydrates choices from TurnOutcome and marks only an adjacent exact id selected", async () => {
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      proposed_workflow: null,
      auto_accept: false,
      chat_history: [
        {
          sender: "ai",
          content: "Which account should I use?",
          created_at: "2026-08-15T00:00:01Z",
          narrative_payload: narrativePayload(false),
          turn_outcome: {
            response_kind: "clarify",
            connected_account_choices: choices,
          },
        },
        {
          sender: "user",
          content: "goac_1",
          created_at: "2026-08-15T00:00:02Z",
        },
      ],
    };

    await renderChat();

    expect(screen.getByText("Choose a Google account")).toBeTruthy();
    expect(screen.getAllByText("Google Sheets")).toHaveLength(2);
    expect(screen.getByText("Selected")).toBeTruthy();
    expect(
      screen.getByRole<HTMLButtonElement>("button", {
        name: /Connection …goac_1.*Selected/,
      }).disabled,
    ).toBe(true);
  });

  it("keeps payload choices when an older backend omits them from TurnOutcome", async () => {
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      proposed_workflow: null,
      auto_accept: false,
      chat_history: [
        {
          sender: "ai",
          content: "Which account should I use?",
          created_at: "2026-08-15T00:00:01Z",
          narrative_payload: narrativePayload(),
          turn_outcome: { response_kind: "clarify" },
        },
      ],
    };

    await renderChat();

    expect(screen.getAllByText("Google Sheets")).toHaveLength(2);
    expect(screen.getByText("second@example.test")).toBeTruthy();
    expect(screen.getByText("Connection …goac_1")).toBeTruthy();
  });

  it("renders a fresh picker on a recovered selection outcome", async () => {
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      proposed_workflow: null,
      auto_accept: false,
      chat_history: [
        {
          sender: "ai",
          content: "Which account should I use?",
          created_at: "2026-08-15T00:00:01Z",
          narrative_payload: narrativePayload(),
          turn_outcome: {
            response_kind: "clarify",
            connected_account_choices: choices,
          },
        },
        {
          sender: "user",
          content: "goac_1",
          created_at: "2026-08-15T00:00:02Z",
        },
        {
          sender: "ai",
          content: "The turn was interrupted. Please try again.",
          created_at: "2026-08-15T00:00:03Z",
          narrative_payload: narrativePayload(false),
          turn_outcome: {
            response_kind: "recover",
            connected_account_choices: choices,
          },
        },
      ],
    };

    await renderChat();

    const retry = screen.getAllByRole<HTMLButtonElement>("button", {
      name: /Connection …goac_1/,
    });
    expect(retry[retry.length - 1]?.disabled).toBe(false);
    expect(screen.queryByText("goac_1")).toBeNull();
  });

  it("does not infer a selection from free text and keeps stale rows disabled", async () => {
    historyResponse.data = {
      workflow_copilot_chat_id: "chat-1",
      proposed_workflow: null,
      auto_accept: false,
      chat_history: [
        {
          sender: "ai",
          content: "Which account should I use?",
          created_at: "2026-08-15T00:00:01Z",
          narrative_payload: narrativePayload(),
        },
        {
          sender: "user",
          content: "use the first account",
          created_at: "2026-08-15T00:00:02Z",
        },
      ],
    };

    await renderChat();

    expect(screen.queryByText("Selected")).toBeNull();
    expect(
      screen.getByRole<HTMLButtonElement>("button", {
        name: /Connection …goac_1/,
      }).disabled,
    ).toBe(true);
  });

  it("keeps the prior picker disabled after a prose turn settles without a response", async () => {
    await renderChat();
    await finishChoiceAsk();

    await submit("use the first account");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    await act(async () => streamCalls[1]!.resolve());

    expect(screen.queryByText("Selected")).toBeNull();
    expect(
      screen.getByRole<HTMLButtonElement>("button", {
        name: /Connection …goac_1/,
      }).disabled,
    ).toBe(true);
  });

  it("preserves ordinary free-text sends when no structured choices exist", async () => {
    await renderChat();
    await submit("ordinary free text");

    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    expect(streamCalls[0]?.body.message).toBe("ordinary free text");
    expect(screen.queryByLabelText("Connected Google accounts")).toBeNull();
  });
});
