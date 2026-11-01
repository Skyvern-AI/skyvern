import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Status } from "@/api/types";
import { FeatureFlagContext } from "@/hooks/useFeatureFlag";

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

const {
  streamCalls,
  postStreaming,
  cancelPost,
  historyResponse,
  routeParams,
  workflowRunQueryMock,
  watchedRunsById,
  historySelection,
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
    watchedRunsById: new Map<string, unknown>(),
    historySelection: { chatId: "chat_other" },
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

vi.mock("../hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQueries: (ids: readonly string[]) =>
    ids.map((id) => ({ data: watchedRunsById.get(id) })),
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

vi.mock("@/store/WorkflowHasChangesStore", () => {
  const state = { getSaveData: () => saveData, setSaveBlockedReason: () => {} };
  return {
    useWorkflowHasChangesStore: Object.assign(() => state, {
      getState: () => state,
    }),
  };
});

// Real WorkflowCopilotHistory needs an infinite-query + debounced Popover;
// a button standing in for "pick a different past chat" is enough here.
vi.mock("./WorkflowCopilotHistory", () => ({
  WorkflowCopilotHistory: ({
    onSelect,
  }: {
    onSelect: (chat: { workflow_copilot_chat_id: string }) => void;
  }) => (
    <button
      onClick={() =>
        onSelect({ workflow_copilot_chat_id: historySelection.chatId })
      }
    >
      mock-select-history-chat
    </button>
  ),
}));

import { WorkflowCopilotChat } from "./WorkflowCopilotChat";

const BOOLEAN_FLAGS: Record<string, boolean> = {
  WORKFLOW_COPILOT_CODE_BLOCK_MODE: false,
  CODE_BLOCK_ACCESS: false,
};

type ChatProps = {
  workflowRunId?: string | null;
  docked?: boolean;
  portalTarget?: HTMLElement | null;
};

function chatUi(props: ChatProps) {
  return (
    <FeatureFlagContext.Provider value={(name) => BOOLEAN_FLAGS[name]}>
      <WorkflowCopilotChat {...props} />
    </FeatureFlagContext.Provider>
  );
}

// docked renders via createPortal(content, portalTarget) and is null without
// one, so docked tests need a real, body-attached portal target to query into.
function makeDockedProps(props: ChatProps = {}): ChatProps {
  const portalTarget = document.createElement("div");
  document.body.appendChild(portalTarget);
  return { docked: true, portalTarget, ...props };
}

async function renderChat(props: ChatProps = {}) {
  const view = render(chatUi(props));
  await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
  return view;
}

function makeRun(overrides: Record<string, unknown> = {}) {
  return {
    workflow_run_id: "wr_1",
    status: Status.Running,
    created_at: "2026-01-01T00:00:00Z",
    started_at: null,
    finished_at: "2026-01-01T00:00:10Z",
    outputs: null,
    failure_reason: null,
    ...overrides,
  };
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
  watchedRunsById.clear();
  historySelection.chatId = "chat_other";
});

afterEach(() => {
  cleanup();
});

describe("WorkflowCopilotChat — run lifecycle lines", () => {
  it("REGRESSION: a docked chat renders a lifecycle line when its focused run transitions running -> completed", async () => {
    workflowRunQueryMock.mockReturnValue({ data: makeRun() });
    const props = makeDockedProps({ workflowRunId: "wr_1" });
    const view = await renderChat(props);

    await waitFor(() =>
      expect(screen.getByText("Run started — watching it now.")).toBeTruthy(),
    );

    workflowRunQueryMock.mockReturnValue({
      data: makeRun({
        status: Status.Completed,
        started_at: "2026-01-01T00:00:00Z",
        finished_at: "2026-01-01T00:00:42Z",
      }),
    });
    view.rerender(chatUi(props));

    await waitFor(() =>
      expect(screen.getByText(/Run completed in 0:42\./)).toBeTruthy(),
    );
  });

  it("keeps proposal actions attached to the last real ai message when a lifecycle entry trails it", async () => {
    historyResponse.data = {
      workflow_copilot_chat_id: "chat_1",
      chat_history: [
        {
          sender: "user",
          content: "build something",
          created_at: "2026-01-01T00:00:00Z",
        },
        {
          sender: "ai",
          content: "Here's a draft.",
          created_at: "2026-01-01T00:00:01Z",
        },
      ],
      proposed_workflow: saveData,
      auto_accept: false,
    };
    workflowRunQueryMock.mockReturnValue({ data: makeRun() });
    const props = makeDockedProps({ workflowRunId: "wr_1" });
    const view = await renderChat(props);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy(),
    );
    await waitFor(() =>
      expect(screen.getByText("Run started — watching it now.")).toBeTruthy(),
    );

    workflowRunQueryMock.mockReturnValue({
      data: makeRun({ status: Status.Completed }),
    });
    view.rerender(chatUi(props));

    await waitFor(() =>
      expect(screen.getByText(/Run completed in/)).toBeTruthy(),
    );
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
  });

  it("renders no lifecycle UI for a non-docked (legacy) chat even with a route-level run id and live data, and never queries it", async () => {
    routeParams.current = {
      workflowPermanentId: "wpid_1",
      workflowRunId: "wr_route",
    };
    workflowRunQueryMock.mockReturnValue({
      data: makeRun({ workflow_run_id: "wr_route" }),
    });
    await renderChat({ docked: false });

    // Give any (incorrect) announcement a chance to land before asserting absence.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText(/watching it now/)).toBeNull();

    // REGRESSION: an options object that omits workflowRunId lets
    // useWorkflowRunQuery fall back to the route's own :workflowRunId and poll it.
    expect(workflowRunQueryMock.mock.calls.length).toBeGreaterThan(0);
    for (const call of workflowRunQueryMock.mock.calls) {
      expect(call[0]).toEqual({ workflowRunId: undefined });
    }
  });

  it("New chat wipes a rendered lifecycle line", async () => {
    workflowRunQueryMock.mockReturnValue({ data: makeRun() });
    await renderChat(makeDockedProps({ workflowRunId: "wr_1" }));

    await waitFor(() =>
      expect(screen.getByText("Run started — watching it now.")).toBeTruthy(),
    );

    fireEvent.click(screen.getByRole("button", { name: /New chat/i }));

    expect(screen.queryByText(/watching it now/)).toBeNull();
  });

  it("REGRESSION: switching to a different past chat drops the lifecycle line instead of carrying it forward", async () => {
    workflowRunQueryMock.mockReturnValue({ data: makeRun() });
    await renderChat(makeDockedProps({ workflowRunId: "wr_1" }));

    await waitFor(() =>
      expect(screen.getByText("Run started — watching it now.")).toBeTruthy(),
    );

    historyResponse.data = {
      workflow_copilot_chat_id: "chat_other",
      chat_history: [
        {
          sender: "ai",
          content: "An old unrelated reply.",
          created_at: "2025-01-01T00:00:00Z",
        },
      ],
      proposed_workflow: null,
      auto_accept: false,
    };
    fireEvent.click(
      screen.getByRole("button", { name: "mock-select-history-chat" }),
    );

    await waitFor(() =>
      expect(screen.getByText("An old unrelated reply.")).toBeTruthy(),
    );
    expect(screen.queryByText(/watching it now/)).toBeNull();
  });
});

const TEST_RUN = "wr_test";
const REPLY_AT = "2026-10-30T16:50:32.751246";
const INSIDE_TURN = "2026-10-30T16:50:30.000000";
const AFTER_REPLY = "2026-10-30T16:50:44.159261";

function testRun(overrides: Record<string, unknown> = {}) {
  return makeRun({
    workflow_run_id: TEST_RUN,
    status: Status.Paused,
    started_at: "2026-10-30T16:50:18.670262",
    finished_at: null,
    ...overrides,
  });
}

function failedTestRun(finishedAt: string) {
  return testRun({
    status: Status.Failed,
    finished_at: finishedAt,
    failure_reason: "final block failed",
  });
}

function pausedTestNarrative() {
  return {
    turnId: "turn-1",
    turnIndex: 0,
    mode: "build",
    designStarted: false,
    designEnded: true,
    draft: null,
    blocks: [],
    terminal: "response",
    terminalMessage: "The test is paused at the approval step.",
    narrativeSummary: "Paused at approval.",
    priorBlockCount: null,
    designActivity: [],
    startedAt: null,
    endedAt: null,
    turnFacts: {
      factsAvailable: true,
      evaluationState: "not_evaluated",
      runId: TEST_RUN,
      runCompleted: false,
      terminalCause: null,
      blocksRunThisTurn: 1,
      ranCleanOnCurrentSource: false,
    },
  };
}

async function startTurn() {
  fireEvent.change(screen.getByRole("textbox"), {
    target: { value: "test it end to end" },
  });
  await act(async () => {
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
  });
  await waitFor(() => expect(streamCalls).toHaveLength(1));
}

async function endTurnWithoutRunOutcome() {
  await act(async () => {
    streamCalls[0]!.onMessage({
      type: "response",
      workflow_copilot_chat_id: "chat_1",
      message: "The test is paused at the approval step.",
      updated_workflow: null,
      response_time: REPLY_AT,
      proposal_disposition: "no_proposal",
      narrative_payload: pausedTestNarrative(),
    });
    streamCalls[0]!.resolve();
  });
}

async function finishTurnPausedWithoutRunOutcome() {
  await startTurn();
  await endTurnWithoutRunOutcome();
}

async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("WorkflowCopilotChat — a test left running when its turn reported no run_outcome", () => {
  it("stays silent on both watchers when the focused test ended inside its turn", async () => {
    watchedRunsById.set(TEST_RUN, testRun({ status: Status.Running }));
    const props = makeDockedProps({ workflowRunId: TEST_RUN });
    const view = await renderChat(props);
    await startTurn();
    workflowRunQueryMock.mockReturnValue({
      data: testRun({ status: Status.Running }),
    });
    view.rerender(chatUi(props));
    await endTurnWithoutRunOutcome();

    workflowRunQueryMock.mockReturnValue({
      data: failedTestRun(INSIDE_TURN),
    });
    watchedRunsById.set(TEST_RUN, failedTestRun(INSIDE_TURN));
    view.rerender(chatUi(props));
    await settle();

    expect(screen.queryByText(/run failed after/i)).toBeNull();
    expect(screen.queryByText(/watching it now/)).toBeNull();
  });

  it("announces once when the test ends after the reply", async () => {
    watchedRunsById.set(TEST_RUN, testRun());
    const props = makeDockedProps();
    const view = await renderChat(props);
    await finishTurnPausedWithoutRunOutcome();
    view.rerender(chatUi(props));
    await settle();
    expect(screen.queryByText(/run failed after/i)).toBeNull();

    watchedRunsById.set(TEST_RUN, failedTestRun(AFTER_REPLY));
    view.rerender(chatUi(props));

    await waitFor(() =>
      expect(screen.getAllByText(/^Test run failed after 0:25/)).toHaveLength(
        1,
      ),
    );
  });

  it("stays silent when the test ended while another chat was open", async () => {
    watchedRunsById.set(TEST_RUN, testRun());
    const props = makeDockedProps();
    const view = await renderChat(props);
    await finishTurnPausedWithoutRunOutcome();
    view.rerender(chatUi(props));
    await settle();

    historyResponse.data = {
      workflow_copilot_chat_id: "chat_other",
      chat_history: [
        {
          sender: "ai",
          content: "An old unrelated reply.",
          created_at: "2025-01-01T00:00:00Z",
        },
      ],
      proposed_workflow: null,
      auto_accept: false,
    };
    fireEvent.click(
      screen.getByRole("button", { name: "mock-select-history-chat" }),
    );
    await waitFor(() =>
      expect(screen.getByText("An old unrelated reply.")).toBeTruthy(),
    );

    watchedRunsById.set(TEST_RUN, failedTestRun(AFTER_REPLY));
    historySelection.chatId = "chat_1";
    historyResponse.data = {
      workflow_copilot_chat_id: "chat_1",
      chat_history: [
        {
          sender: "user",
          content: "test it end to end",
          created_at: "2026-10-30T16:50:10.000000",
        },
        {
          sender: "ai",
          content: "The test is paused at the approval step.",
          created_at: REPLY_AT,
          narrative_payload: pausedTestNarrative(),
        },
      ],
      proposed_workflow: null,
      auto_accept: false,
    };
    fireEvent.click(
      screen.getByRole("button", { name: "mock-select-history-chat" }),
    );
    await waitFor(() =>
      expect(screen.queryByText("An old unrelated reply.")).toBeNull(),
    );
    view.rerender(chatUi(props));
    await settle();

    expect(screen.queryByText(/run failed after/i)).toBeNull();
  });
});
