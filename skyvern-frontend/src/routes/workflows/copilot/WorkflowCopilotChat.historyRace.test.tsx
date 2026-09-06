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

import type { QuestionInteraction } from "./workflowCopilotTypes";

type HistoryData = {
  question_interactions?: QuestionInteraction[];
  pending_question_cancel_token?: string | null;
  workflow_copilot_chat_id: string | null;
  chat_history: unknown[];
  proposed_workflow: Record<string, unknown> | null;
  auto_accept: boolean;
};

type StreamCall = {
  onMessage: (payload: unknown) => boolean;
  resolve: () => void;
  reject: (error: unknown) => void;
};

// Only chat-history GETs are deferred (held here so a test controls
// isLoadingHistory); every other GET resolves immediately. Streams are held
// open too, so a test drives each one to its own ending.
const {
  streamCalls,
  postStreaming,
  cancelPost,
  historyQueue,
  historyRejects,
  historyParams,
  workflowGets,
  hasLocalChanges,
  historySignals,
  boolFlags,
  routeWpid,
  announceRef,
} = vi.hoisted(() => {
  const calls: StreamCall[] = [];
  return {
    streamCalls: calls,
    postStreaming: vi.fn(
      (
        _path: string,
        _body: unknown,
        onMessage: (payload: unknown) => boolean,
      ) =>
        new Promise<void>((resolve, reject) => {
          calls.push({ onMessage, resolve, reject });
        }),
    ),
    cancelPost: vi.fn().mockResolvedValue({}),
    historyQueue: [] as Array<(resp: { data: HistoryData }) => void>,
    historyRejects: [] as Array<(reason?: unknown) => void>,
    workflowGets: [] as string[],
    hasLocalChanges: { current: false },
    historyParams: [] as Array<Record<string, unknown> | undefined>,
    historySignals: [] as Array<AbortSignal | undefined>,
    boolFlags: { current: {} as Record<string, boolean> },
    routeWpid: { current: "wpid_1" },
    announceRef: { current: null as ((message: unknown) => void) | null },
  };
});

vi.mock("@/api/sse", () => ({
  getSseClient: vi.fn().mockResolvedValue({ postStreaming }),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn().mockResolvedValue({
    get: vi.fn(
      (
        path: string,
        config?: { params?: Record<string, unknown>; signal?: AbortSignal },
      ) => {
        if (path === "/workflow/copilot/chat-history") {
          historyParams.push(config?.params);
          historySignals.push(config?.signal);
          return new Promise((resolve, reject) => {
            historyQueue.push(resolve as (resp: { data: HistoryData }) => void);
            historyRejects.push(reject);
          });
        }
        if (path.startsWith("/workflows/")) {
          workflowGets.push(path);
          return Promise.resolve({ data: { workflow_permanent_id: "wpid_1" } });
        }
        return Promise.resolve({ data: [] });
      },
    ),
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
      workflowPermanentId: routeWpid.current,
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

vi.mock("@/store/WorkflowHasChangesStore", () => {
  const useWorkflowHasChangesStore = () => ({
    getSaveData: () => ({
      title: "Test WF",
      workflow: {
        workflow_id: "wf_1",
        workflow_permanent_id: "wpid_1",
        description: "",
        totp_verification_url: null,
        is_saved_task: false,
        status: "published",
      },
      settings: {},
      parameters: [],
      blocks: [],
      workflowDefinitionVersion: 1,
    }),
  });
  // The real store is a zustand store; the recovery re-read reads hasChanges
  // off getState() so it never overwrites unsaved local edits.
  useWorkflowHasChangesStore.getState = () => ({
    hasChanges: hasLocalChanges.current,
  });
  return { useWorkflowHasChangesStore };
});

vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: undefined }),
}));

vi.mock("./useRunLifecycleAnnouncements", () => ({
  useRunLifecycleAnnouncements: ({
    announce,
  }: {
    announce: (message: unknown) => void;
  }) => {
    announceRef.current = announce;
  },
}));

// The real selector needs an infinite-query + debounced Popover; a plain button
// standing in for "pick a different past chat" is enough to drive the switch.
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

import capturedHistory from "./recoveryPoll.chatHistory.fixture.json";
import { WorkflowCopilotChat } from "./WorkflowCopilotChat";

const normalize = (value: string): string => value.replace(/\s+/g, " ").trim();

const narrativePayload = (
  overrides: Record<string, unknown> = {},
): Record<string, unknown> => ({
  turnId: "turn-hist",
  turnIndex: 0,
  mode: "build",
  responseType: "REPLY",
  cancelled: false,
  proposalDisposition: "no_proposal",
  designStarted: true,
  designEnded: true,
  draft: null,
  blocks: [],
  terminal: "response",
  terminalMessage: "All set.",
  narrativeSummary: "All set.",
  priorBlockCount: 0,
  designActivity: [],
  startedAt: "2026-07-15T00:00:00Z",
  endedAt: "2026-07-15T00:00:05Z",
  ...overrides,
});

const aiHistoryMessage = (
  narrative_payload: Record<string, unknown> | null,
  content = "prior turn",
) => ({
  sender: "ai" as const,
  content,
  created_at: "2026-07-15T00:00:00Z",
  narrative_payload,
  turn_outcome: null,
});

const historyData = (overrides: Partial<HistoryData> = {}): HistoryData => ({
  workflow_copilot_chat_id: "chat-1",
  chat_history: [],
  proposed_workflow: null,
  auto_accept: false,
  ...overrides,
});

const portalTargets: HTMLElement[] = [];

function chatUi(props: {
  docked?: boolean;
  requiresLiveBrowser?: boolean;
  isLiveBrowserReady?: boolean;
  portalTarget?: HTMLElement | null;
}) {
  return (
    <FeatureFlagContext.Provider value={(name) => boolFlags.current[name]}>
      <WorkflowCopilotChat
        docked={props.docked ?? false}
        portalTarget={props.portalTarget}
        requiresLiveBrowser={props.requiresLiveBrowser}
        isLiveBrowserReady={props.isLiveBrowserReady}
      />
    </FeatureFlagContext.Provider>
  );
}

async function renderChat(
  props: Parameters<typeof chatUi>[0] = {},
): Promise<ReturnType<typeof render>> {
  let portalTarget: HTMLElement | undefined;
  if (props.docked) {
    portalTarget = document.createElement("div");
    document.body.appendChild(portalTarget);
    portalTargets.push(portalTarget);
  }
  const result = render(chatUi({ ...props, portalTarget }));
  await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
  return result;
}

async function flushHistory(data: HistoryData): Promise<void> {
  await waitFor(() => expect(historyQueue.length).toBeGreaterThan(0));
  const resolve = historyQueue.shift()!;
  historyRejects.shift();
  await act(async () => {
    resolve({ data });
    await Promise.resolve();
  });
}

function textarea(): HTMLTextAreaElement {
  return screen.getByRole("textbox") as HTMLTextAreaElement;
}

async function submit(value: string): Promise<void> {
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
  cancelPost.mockResolvedValue({});
  historyQueue.length = 0;
  historyRejects.length = 0;
  historyParams.length = 0;
  workflowGets.length = 0;
  hasLocalChanges.current = false;
  boolFlags.current = {};
  routeWpid.current = "wpid_1";
  announceRef.current = null;
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
  portalTargets.splice(0).forEach((el) => el.remove());
});

// Item 1 (SKY-12384): during a chat-history SWITCH, a prior chat's action card
// must not stay actionable — its action would post into the OUTGOING chat.
describe("WorkflowCopilotChat — history-race action-card gating (item 1)", () => {
  it("hides the review gate's Accept while a chat switch is loading", async () => {
    await renderChat();
    await flushHistory(
      historyData({
        proposed_workflow: { workflow_id: "wf_p", _copilot_unvalidated: true },
        chat_history: [aiHistoryMessage(null, "Here is a draft.")],
      }),
    );

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy(),
    );

    fireEvent.click(screen.getByText("mock-select-history-chat"));

    // Switch GET is in flight (isLoadingHistory=true): the outgoing gate's
    // actions are gone, so no Accept can apply into the outgoing chat.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Accept" })).toBeNull(),
    );
    expect(cancelPost).not.toHaveBeenCalledWith(
      "/workflow/copilot/apply-proposed-workflow",
      expect.anything(),
    );

    await flushHistory(historyData({ workflow_copilot_chat_id: "chat_other" }));
  });

  it("hides the Confirm chip while a chat switch is loading", async () => {
    await renderChat();
    await flushHistory(
      historyData({
        chat_history: [
          aiHistoryMessage(
            narrativePayload({
              terminalMessage: "Want me to confirm and I'll apply the change?",
              narrativeSummary: "Want me to confirm and I'll apply the change?",
            }),
            "Want me to confirm and I'll apply the change?",
          ),
        ],
      }),
    );

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Confirm" })).toBeTruthy(),
    );

    fireEvent.click(screen.getByText("mock-select-history-chat"));

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Confirm" })).toBeNull(),
    );

    await flushHistory(historyData({ workflow_copilot_chat_id: "chat_other" }));
  });
});

// Item 2 (SKY-12384): a live_browser prompt queued before the initial history
// load must keep one owner — footer while the bubble exists, else the chip.
describe("WorkflowCopilotChat — queued prompt survives initial history load (item 2)", () => {
  it("keeps the queued status + Cancel visible after the history load lands", async () => {
    boolFlags.current = {};
    await renderChat({ requiresLiveBrowser: true, isLiveBrowserReady: false });

    // Queue while the initial history GET is still pending (isLoadingHistory).
    // The bubble owns the footer here.
    await submit("log into the portal");
    await waitFor(() =>
      expect(
        screen.getByText("Prompt queued. Waiting for live browser..."),
      ).toBeTruthy(),
    );
    expect(
      screen.getByRole("button", { name: "Edit queued message" }),
    ).toBeTruthy();

    // History resolves and replaces messages, dropping the queued bubble — the
    // composer chip must pick up the status/Cancel (old code shows neither).
    await flushHistory(
      historyData({ chat_history: [aiHistoryMessage(null, "Earlier chat.")] }),
    );

    expect(
      screen.getByText("Prompt queued. Waiting for live browser..."),
    ).toBeTruthy();
    const cancel = screen.getByRole("button", {
      name: "Edit queued message",
    });

    await act(async () => {
      fireEvent.click(cancel);
    });

    await waitFor(() =>
      expect(
        screen.queryByText("Prompt queued. Waiting for live browser..."),
      ).toBeNull(),
    );
  });
});

// The server keeps the copilot handler running after a client disconnect, so a
// stream that closes with no terminal frame usually still ends in a persisted
// assistant row. The fixture is the last turn of a captured chat-history response.
describe("WorkflowCopilotChat — recovery poll after a non-terminal stream close", () => {
  const capturedAiRow =
    capturedHistory.chat_history[capturedHistory.chat_history.length - 1]!;
  const turnId = capturedAiRow.turn_outcome!.copilot_turn_id;
  // Markdown renders away, and the row runs to a bulleted summary, so only its
  // opening line is matched against normalized rendered text.
  const capturedAiText = normalize(
    capturedAiRow.content.replace(/[`*]/g, "").split(/\n/)[0]!,
  );
  const interruptedText = "This turn was interrupted before it could finish.";

  function renderedText(): string {
    return normalize(document.body.textContent ?? "");
  }

  function historyWithRow(row: Record<string, unknown>): HistoryData {
    return {
      workflow_copilot_chat_id: capturedHistory.workflow_copilot_chat_id,
      chat_history: [...capturedHistory.chat_history.slice(0, -1), row],
      proposed_workflow: null,
      auto_accept: false,
    };
  }

  // The server's own clock, deliberately far behind the client's: correlation
  // must not depend on comparing it to Date.now().
  function recoveredHistory(): HistoryData {
    return historyWithRow({
      ...capturedAiRow,
      created_at: "2025-01-01T00:00:00",
    });
  }

  function interruptedHistory(): HistoryData {
    return historyWithRow({
      sender: "ai",
      content: interruptedText,
      created_at: "2025-01-01T00:00:00",
      narrative_payload: null,
      turn_outcome: {
        response_kind: "recover",
        terminal_reason: "interrupted",
        copilot_turn_id: turnId,
      },
    });
  }

  // A reply persisted with no turn outcome at all: nothing to correlate on but
  // its position and its clock.
  function untaggedHistory(): HistoryData {
    return historyWithRow({
      ...capturedAiRow,
      created_at: new Date(Date.now() + 60_000).toISOString().replace("Z", ""),
      turn_outcome: null,
    });
  }

  // Another tab's turn, persisted after this send: newest row, wrong turn.
  function otherTurnHistory(): HistoryData {
    return historyWithRow({
      ...capturedAiRow,
      created_at: new Date(Date.now() + 60_000).toISOString().replace("Z", ""),
      turn_outcome: { ...capturedAiRow.turn_outcome, copilot_turn_id: "other" },
    });
  }

  async function emitTurnStart(index = 0): Promise<void> {
    await act(async () => {
      streamCalls[index]!.onMessage({
        type: "turn_start",
        turn_id: turnId,
        turn_index: index,
        timestamp: "2026-09-02T06:33:30Z",
      });
      await Promise.resolve();
    });
  }

  async function startTurn(
    chatId: string | null = "chat-1",
  ): Promise<ReturnType<typeof render>> {
    const handle = await renderChat();
    await flushHistory(historyData({ workflow_copilot_chat_id: chatId }));
    await submit("build me a flow");
    await waitFor(() => expect(streamCalls.length).toBe(1));
    await emitTurnStart();
    vi.useFakeTimers();
    return handle;
  }

  async function advance(ms: number): Promise<void> {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  async function closeStreamWithoutTerminal(index = 0): Promise<void> {
    await act(async () => {
      streamCalls[index]!.reject(
        new Error("SSE stream ended without terminal event"),
      );
      await Promise.resolve();
    });
  }

  async function emitTerminalResponse(index = 0): Promise<void> {
    await act(async () => {
      streamCalls[index]!.onMessage({
        type: "response",
        workflow_copilot_chat_id: "chat-1",
        message: "All done.",
        updated_workflow: null,
        response_time: "2026-08-16T02:02:13Z",
        proposal_disposition: "no_proposal",
      });
      streamCalls[index]!.resolve();
      await Promise.resolve();
    });
  }

  async function resolveNextHistory(data: HistoryData): Promise<void> {
    expect(historyQueue.length).toBeGreaterThan(0);
    const resolve = historyQueue.shift()!;
    historyRejects.shift();
    await act(async () => {
      resolve({ data });
      await Promise.resolve();
    });
  }

  it("renders the assistant row the server persisted, replacing the error bubble", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();
    expect(
      screen.getByText(
        "The connection dropped, so Copilot is checking whether this turn finished.",
      ),
    ).toBeTruthy();
    expect(
      screen.queryByText("Sorry, I encountered an error. Please try again."),
    ).toBeNull();
    expect(historyQueue.length).toBe(0);

    await advance(2_000);
    await resolveNextHistory(recoveredHistory());

    expect(renderedText()).toContain(capturedAiText);
    expect(
      screen.queryByText(
        "The connection dropped, so Copilot is checking whether this turn finished.",
      ),
    ).toBeNull();
  });

  it("recovers a brand-new chat's first turn, whose id never arrived", async () => {
    await startTurn(null);
    await closeStreamWithoutTerminal();

    await advance(2_000);
    expect(historyParams[historyParams.length - 1]).toEqual({
      workflow_permanent_id: "wpid_1",
    });

    await resolveNextHistory(recoveredHistory());
    expect(renderedText()).toContain(capturedAiText);
  });

  it("can answer a recovered first question whose chat id never arrived", async () => {
    await startTurn(null);
    await closeStreamWithoutTerminal();
    await advance(2_000);
    const interaction: QuestionInteraction = {
      interaction_id: "recovered-question",
      turn_id: turnId,
      tool_call_id: "call",
      status: "pending",
      response: null,
      created_at: "2026-09-04T00:00:00Z",
      resolved_at: null,
      parts: [
        {
          part_id: "format",
          prompt: "Which format?",
          choices: [{ choice_id: "csv", text: "CSV" }],
        },
      ],
    };
    await resolveNextHistory({
      ...recoveredHistory(),
      chat_history: [],
      question_interactions: [interaction],
      pending_question_cancel_token: "stop",
    });
    cancelPost.mockResolvedValueOnce({
      data: { ...interaction, status: "resolved", response: { skipped: true } },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Skip" }));
    });
    expect(cancelPost).toHaveBeenCalledWith(
      "/workflow/copilot/question-response",
      {
        workflow_copilot_chat_id: capturedHistory.workflow_copilot_chat_id,
        interaction_id: "recovered-question",
        skipped: true,
      },
    );
  });

  it("keeps polling while the only assistant row belongs to another turn", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();

    await advance(2_000);
    await resolveNextHistory(otherTurnHistory());
    expect(renderedText()).not.toContain(capturedAiText);

    await advance(3_000);
    await resolveNextHistory(recoveredHistory());
    expect(renderedText()).toContain(capturedAiText);
  });

  it("keeps reading past the reconcile threshold and lets the real reply supersede the interrupted row", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();

    // Each pass is one poll cycle; 45 of them carry virtual time past the
    // server's ~1320s reconcile threshold with nothing persisted yet.
    const startedAt = Date.now();
    for (let i = 0; i < 45; i += 1) {
      await advance(30_000);
      await resolveNextHistory(historyData({ chat_history: [] }));
    }
    expect(Date.now() - startedAt).toBeGreaterThan(1_320_000);

    await advance(30_000);
    await resolveNextHistory(interruptedHistory());
    expect(renderedText()).toContain(interruptedText);

    await advance(30_000);
    await resolveNextHistory(recoveredHistory());
    expect(renderedText()).toContain(capturedAiText);
  });

  it("reads on its own timer only, and stops once the budget elapses", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();

    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      document.dispatchEvent(new Event("visibilitychange"));
      await Promise.resolve();
    });
    expect(historyQueue.length).toBe(0);

    // Each pass is one poll cycle; enough of them carry virtual time past the
    // poll's own 1_500_000ms budget with nothing ever persisted. The budget
    // fires on its own timer, so the last cycle stops mid-loop rather than
    // after a fixed count.
    const startedAt = Date.now();
    let reads = 0;
    for (let i = 0; i < 64; i += 1) {
      await advance(30_000);
      if (historyQueue.length === 0) {
        break;
      }
      await resolveNextHistory(historyData({ chat_history: [] }));
      reads += 1;
    }
    expect(reads).toBeGreaterThan(40);
    expect(Date.now() - startedAt).toBeGreaterThanOrEqual(1_500_000);

    await advance(300_000);
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      document.dispatchEvent(new Event("visibilitychange"));
      await Promise.resolve();
    });
    expect(historyQueue.length).toBe(0);
  });

  it("keeps an earlier turn's recovery alive across a later send", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();

    await submit("another one");
    for (let i = 0; i < 20 && streamCalls.length < 2; i += 1) {
      await advance(10);
    }
    expect(streamCalls.length).toBe(2);
    await emitTerminalResponse(1);

    await advance(2_000);
    await resolveNextHistory(recoveredHistory());
    expect(renderedText()).toContain(capturedAiText);
  });

  it("does not re-read history when the stream ends on a terminal frame", async () => {
    await startTurn();
    await emitTerminalResponse();

    await advance(30_000);
    expect(historyQueue.length).toBe(0);
  });

  it("does not re-read history when the user stopped the turn", async () => {
    await startTurn();
    await act(async () => {
      fireEvent.keyDown(document, { key: "Escape" });
      await Promise.resolve();
    });
    expect(cancelPost).toHaveBeenCalledWith(
      "/workflow/copilot/cancel",
      expect.anything(),
    );
    await closeStreamWithoutTerminal();

    await advance(30_000);
    expect(historyQueue.length).toBe(0);
  });

  it("does not re-read history for a stream that never announced a turn", async () => {
    // Without a turn id there is nothing to correlate a row against, so the
    // poll must not arm at all rather than match on position.
    await renderChat();
    await flushHistory(historyData());
    await submit("build me a flow");
    await waitFor(() => expect(streamCalls.length).toBe(1));
    vi.useFakeTimers();
    await closeStreamWithoutTerminal();

    await advance(30_000);
    expect(historyQueue.length).toBe(0);
  });

  it("gives up and shows the plain failure once reads keep failing", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();
    expect(renderedText()).toContain(
      "Copilot is checking whether this turn finished",
    );

    // The usual cause of a severed stream is a client that lost the network, so
    // every recovery read fails too; it must not hold the notice for 25 minutes.
    for (let i = 0; i < 3; i += 1) {
      await advance(30_000);
      expect(historyQueue.length).toBeGreaterThan(0);
      historyQueue.shift();
      const failRead = historyRejects.shift()!;
      await act(async () => {
        failRead(new Error("offline"));
        await Promise.resolve();
      });
    }

    await advance(120_000);
    expect(historyQueue.length).toBe(0);
    expect(renderedText()).not.toContain(
      "Copilot is checking whether this turn finished",
    );
    expect(renderedText()).toContain("Sorry, I encountered an error");
  });

  it("does not re-read history for a turn whose chat the user left mid-stream", async () => {
    await startTurn();
    await act(async () => {
      fireEvent.click(screen.getByText("mock-select-history-chat"));
      await Promise.resolve();
    });
    const switchLoad = historyQueue.shift()!;
    await act(async () => {
      switchLoad({
        data: historyData({ workflow_copilot_chat_id: "chat_other" }),
      });
      await Promise.resolve();
    });
    await closeStreamWithoutTerminal();

    await advance(30_000);
    expect(historyQueue.length).toBe(0);
  });

  it("stops the poll when the route moves to another workflow", async () => {
    const handle = await startTurn();
    await closeStreamWithoutTerminal();

    routeWpid.current = "wpid_2";
    await act(async () => {
      handle.rerender(chatUi({}));
      await Promise.resolve();
    });
    await advance(1);
    await resolveNextHistory(
      historyData({ workflow_copilot_chat_id: "chat_other_workflow" }),
    );

    await advance(30_000);
    expect(historyQueue.length).toBe(0);
  });

  it("arms the poll on the connected-account refresh, which is the same severed stream", async () => {
    await renderChat();
    await flushHistory(
      historyData({
        chat_history: [
          {
            sender: "ai",
            content: "Which account should I use?",
            created_at: "2026-07-15T00:00:00Z",
            narrative_payload: narrativePayload({ turnId: "turn-choice" }),
            turn_outcome: {
              response_kind: "clarify",
              connected_account_choices: [
                {
                  connection_id: "conn_12345678",
                  name: "Work account",
                  state: "active",
                  email_address: "work@example.com",
                },
              ],
            },
          },
        ],
      }),
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Work account/ }));
    });
    await waitFor(() => expect(streamCalls.length).toBe(1));
    await emitTurnStart();
    vi.useFakeTimers();
    await closeStreamWithoutTerminal();

    // The catch branch's own in-place refresh reads once, far too early.
    await resolveNextHistory(historyData({ chat_history: [] }));

    await advance(2_000);
    await resolveNextHistory(recoveredHistory());
    expect(renderedText()).toContain(capturedAiText);
  });

  it("does not arm when a chat switch lands during the connected-account refresh", async () => {
    await renderChat();
    await flushHistory(
      historyData({
        chat_history: [
          {
            sender: "ai",
            content: "Which account should I use?",
            created_at: "2026-07-15T00:00:00Z",
            narrative_payload: narrativePayload({ turnId: "turn-choice" }),
            turn_outcome: {
              response_kind: "clarify",
              connected_account_choices: [
                {
                  connection_id: "conn_12345678",
                  name: "Work account",
                  state: "active",
                  email_address: "work@example.com",
                },
              ],
            },
          },
        ],
      }),
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Work account/ }));
    });
    await waitFor(() => expect(streamCalls.length).toBe(1));
    await emitTurnStart();
    vi.useFakeTimers();
    await closeStreamWithoutTerminal();

    // The refresh forgives its own single generation bump. Here the user also
    // leaves for another chat while it is still awaiting, so the turn belongs
    // to a chat that is no longer on screen and must not arm.
    await act(async () => {
      fireEvent.click(screen.getByText("mock-select-history-chat"));
      await Promise.resolve();
    });
    while (historyQueue.length > 0) {
      await act(async () => {
        historyQueue.shift()!({
          data: historyData({ workflow_copilot_chat_id: "chat_other" }),
        });
        await Promise.resolve();
      });
    }

    await advance(30_000);
    expect(historyQueue.length).toBe(0);
  });

  it("recovers a reply the server persisted with no turn outcome", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();

    await advance(2_000);
    await resolveNextHistory(untaggedHistory());
    expect(renderedText()).toContain(capturedAiText);

    await advance(30_000);
    expect(historyQueue.length).toBe(0);
  });

  it("will not adopt an untagged row while polling by workflow id", async () => {
    // No chat id means the read resolves "the workflow's latest chat", which can
    // be another tab's. An untagged row there carries nothing tying it to this
    // turn, so only an id match may end the poll.
    await startTurn(null);
    await closeStreamWithoutTerminal();

    await advance(2_000);
    expect(historyParams[historyParams.length - 1]).toEqual({
      workflow_permanent_id: "wpid_1",
    });
    await resolveNextHistory(untaggedHistory());
    expect(renderedText()).not.toContain(capturedAiText);

    await advance(3_000);
    await resolveNextHistory(recoveredHistory());
    expect(renderedText()).toContain(capturedAiText);
  });

  it("aborts the read in flight when the budget elapses", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();

    await advance(2_000);
    const inFlight = historySignals[historySignals.length - 1];
    expect(inFlight?.aborted).toBe(false);

    // Past the budget with the read still unresolved: the deadline must reach
    // the request, not just the timer that scheduled it.
    await advance(1_500_000);
    expect(inFlight?.aborted).toBe(true);

    // The aborted read rejects; that rejection must not reschedule the ladder.
    await act(async () => {
      historyQueue.shift();
      await Promise.resolve();
    });
    await advance(30_000);
    expect(historyQueue.length).toBe(0);
  });

  it("stays on the chat it first resolved when polling by workflow id", async () => {
    await startTurn(null);
    await closeStreamWithoutTerminal();

    await advance(2_000);
    expect(historyParams[historyParams.length - 1]).toEqual({
      workflow_permanent_id: "wpid_1",
    });
    // Another tab can create a newer chat, and "the workflow's latest" would
    // then resolve away from this turn's chat for every later read.
    await resolveNextHistory(
      historyData({
        workflow_copilot_chat_id: "chat_of_this_turn",
        chat_history: [],
      }),
    );

    await advance(3_000);
    expect(historyParams[historyParams.length - 1]).toEqual({
      workflow_copilot_chat_id: "chat_of_this_turn",
    });
  });

  it("does not apply history fetched before a send that came and went", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();

    await advance(2_000);
    expect(historyQueue.length).toBe(1);
    const staleRead = historyQueue.shift()!;

    // A whole send begins and ends while that read is outstanding, so the
    // in-flight flag is false at both ends of it.
    await submit("another one");
    for (let i = 0; i < 20 && streamCalls.length < 2; i += 1) {
      await advance(10);
    }
    expect(streamCalls.length).toBe(2);
    await act(async () => {
      streamCalls[1]!.resolve();
      await Promise.resolve();
    });

    await act(async () => {
      staleRead({ data: recoveredHistory() });
      await Promise.resolve();
    });
    // Applying it would rebuild the transcript without the send that just ran.
    expect(renderedText()).toContain("another one");

    await advance(30_000);
    expect(historyQueue.length).toBeGreaterThan(0);
  });

  it("drops the refreshing notice when the budget runs out", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();
    expect(renderedText()).toContain(
      "Copilot is checking whether this turn finished",
    );

    for (let i = 0; i < 64; i += 1) {
      await advance(30_000);
      if (historyQueue.length === 0) {
        break;
      }
      await resolveNextHistory(historyData({ chat_history: [] }));
    }

    // Recovery has stopped, so a notice saying it is still refreshing is stale.
    expect(renderedText()).not.toContain(
      "Copilot is checking whether this turn finished",
    );
    expect(renderedText()).toContain("Sorry, I encountered an error");
  });

  it("stops watching for a supersede well short of the budget", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();

    // A turn cancelled by a worker drain writes this row and never finishes, so
    // waiting out the budget would have every open chat polling through a
    // deploy.
    await advance(2_000);
    await resolveNextHistory(interruptedHistory());
    expect(renderedText()).toContain(interruptedText);

    let reads = 0;
    for (let i = 0; i < 30; i += 1) {
      await advance(30_000);
      if (historyQueue.length === 0) {
        break;
      }
      await resolveNextHistory(interruptedHistory());
      reads += 1;
    }
    expect(reads).toBeLessThan(10);
    expect(historyQueue.length).toBe(0);
  });

  it("re-reads the workflow once a recovered turn lands", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();

    await advance(2_000);
    await resolveNextHistory(recoveredHistory());
    expect(renderedText()).toContain(capturedAiText);

    // The turn may have committed a build the editor never saw, and its terminal
    // frame never arrived to apply it; a stale graph here is what a later save
    // would write back over the commit.
    await act(async () => {
      await Promise.resolve();
    });
    expect(workflowGets.some((url) => url === "/workflows/wpid_1")).toBe(true);
  });

  it("leaves the workflow alone when the editor has unsaved edits", async () => {
    hasLocalChanges.current = true;
    await startTurn();
    await closeStreamWithoutTerminal();

    await advance(2_000);
    await resolveNextHistory(recoveredHistory());
    expect(renderedText()).toContain(capturedAiText);

    await act(async () => {
      await Promise.resolve();
    });
    // Losing edits the user can see is worse than the stale graph being fixed.
    expect(workflowGets).toEqual([]);
  });

  it("re-arming the same turn replaces its ladder instead of adding one", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();

    await submit("another one");
    for (let i = 0; i < 20 && streamCalls.length < 2; i += 1) {
      await advance(10);
    }
    expect(streamCalls.length).toBe(2);
    await emitTurnStart(1);
    await closeStreamWithoutTerminal(1);

    await advance(2_500);
    expect(historyQueue.length).toBe(1);
  });

  it("keeps this chat's run-lifecycle lines when the recovered row lands", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();
    await act(async () => {
      announceRef.current!({
        id: "run-lifecycle-wr_1-start",
        sender: "ai",
        kind: "run_lifecycle",
        content: "Run started - watching it now.",
      });
      await Promise.resolve();
    });

    await advance(2_000);
    await resolveNextHistory(recoveredHistory());

    expect(renderedText()).toContain(capturedAiText);
    expect(screen.getByText("Run started - watching it now.")).toBeTruthy();
  });

  it("discards a response that lands after the user switched chats", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();
    await advance(2_000);
    expect(historyQueue.length).toBe(1);
    const stalePoll = historyQueue.shift()!;

    await act(async () => {
      fireEvent.click(screen.getByText("mock-select-history-chat"));
      await Promise.resolve();
    });
    const switchLoad = historyQueue.shift()!;
    await act(async () => {
      switchLoad({
        data: historyData({
          workflow_copilot_chat_id: "chat_other",
          chat_history: [aiHistoryMessage(null, "Other chat reply.")],
        }),
      });
      await Promise.resolve();
    });

    await act(async () => {
      stalePoll({ data: recoveredHistory() });
      await Promise.resolve();
    });

    expect(screen.getByText("Other chat reply.")).toBeTruthy();
    expect(renderedText()).not.toContain(capturedAiText);
  });
});

describe("WorkflowCopilotChat — question transport", () => {
  it("hydrates the persisted question and routes the composer to its interaction", async () => {
    await renderChat();
    await flushHistory(
      historyData({
        question_interactions: [
          {
            interaction_id: "interaction",
            turn_id: "turn-ask",
            tool_call_id: "call",
            status: "pending",
            response: null,
            created_at: "2026-09-04T00:00:00Z",
            resolved_at: null,
            parts: [{ part_id: "part", prompt: "Which store?", choices: [] }],
          },
        ],
        pending_question_cancel_token: "cancel",
        chat_history: [],
      }),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("textbox", { name: "Your response" }),
      ).toBeTruthy(),
    );
    await act(async () => {
      announceRef.current?.({
        id: "lifecycle-1",
        sender: "ai",
        content: "Run started",
        kind: "run_lifecycle",
      });
    });
    cancelPost.mockResolvedValueOnce({
      data: {
        interaction_id: "interaction",
        turn_id: "turn-ask",
        tool_call_id: "call",
        status: "resolved",
        parts: [],
        response: { text: "why do you need this?" },
      },
    });
    const composer = screen.getByPlaceholderText("Answer Copilot…");
    fireEvent.change(composer, { target: { value: "why do you need this?" } });
    await act(async () => {
      fireEvent.keyDown(composer, { key: "Enter" });
    });
    await waitFor(() => expect(cancelPost).toHaveBeenCalled());
    expect(postStreaming).not.toHaveBeenCalled();
    expect(cancelPost.mock.calls[0]?.[0]).toBe(
      "/workflow/copilot/question-response",
    );
    expect(cancelPost.mock.calls[0]?.[1]).toEqual({
      workflow_copilot_chat_id: "chat-1",
      interaction_id: "interaction",
      text: "why do you need this?",
    });
  });
});
