import {
  canonicalRecoveriesByWorkflow,
  WorkflowCopilotChat,
} from "./WorkflowCopilotChat";
import { toast } from "@/components/ui/use-toast";
import type { EditorStateSnapshot } from "../editor/editorStateSnapshot";
import type { ComponentProps } from "react";
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
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";

import type {
  QuestionInteraction,
  WorkflowCopilotCredentialRequiredUpdate,
} from "./workflowCopilotTypes";

type HistoryData = {
  request_turn_id?: string | null;
  question_interactions?: QuestionInteraction[];
  pending_question_cancel_token?: string | null;
  pending_credential_requests?: WorkflowCopilotCredentialRequiredUpdate[];
  workflow_copilot_chat_id: string | null;
  chat_history: unknown[];
  proposed_workflow: Record<string, unknown> | null;
  proposed_workflow_metadata?: Record<string, unknown> | null;
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
  workflowGet,
  workflowResponse,
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
    workflowGet: vi.fn(),
    workflowResponse: {
      current: { workflow_id: "wf_recovered", workflow_permanent_id: "wpid_1" },
    },
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
          return workflowGet();
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
      settings: { proxyLocation: null },
      parameters: [],
      blocks: [],
      workflowDefinitionVersion: 1,
    }),
  });
  // Ordinary history recovery reads hasChanges from the zustand store so it
  // preserves unsaved edits when no persisted outcome needs reconciliation.
  useWorkflowHasChangesStore.getState = () => ({
    hasChanges: hasLocalChanges.current,
    setSaveBlockedReason: () => {},
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
    <>
      <button
        onClick={() => onSelect({ workflow_copilot_chat_id: "chat_other" })}
      >
        mock-select-history-chat
      </button>
      <button onClick={() => onSelect({ workflow_copilot_chat_id: "chat-1" })}>
        mock-select-history-chat-1
      </button>
    </>
  ),
}));

import capturedHistory from "./recoveryPoll.chatHistory.fixture.json";

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
  captureEditorState?: NonNullable<
    ComponentProps<typeof WorkflowCopilotChat>
  >["captureEditorState"];
  restoreEditorState?: NonNullable<
    ComponentProps<typeof WorkflowCopilotChat>
  >["restoreEditorState"];
  onWorkflowUpdate?: NonNullable<
    ComponentProps<typeof WorkflowCopilotChat>
  >["onWorkflowUpdate"];
}) {
  return (
    <FeatureFlagContext.Provider value={(name) => boolFlags.current[name]}>
      <WorkflowCopilotChat
        docked={props.docked ?? false}
        portalTarget={props.portalTarget}
        requiresLiveBrowser={props.requiresLiveBrowser}
        isLiveBrowserReady={props.isLiveBrowserReady}
        onWorkflowUpdate={props.onWorkflowUpdate}
        captureEditorState={props.captureEditorState}
        restoreEditorState={props.restoreEditorState}
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
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
  sessionStorage.clear();
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
  workflowGet
    .mockReset()
    .mockImplementation(() =>
      Promise.resolve({ data: workflowResponse.current }),
    );
  workflowResponse.current = {
    workflow_id: "wf_recovered",
    workflow_permanent_id: "wpid_1",
  };
  hasLocalChanges.current = false;
  boolFlags.current = {};
  routeWpid.current = "wpid_1";
  announceRef.current = null;
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
  canonicalRecoveriesByWorkflow.clear();
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
      { timeout: 30_000, signal: expect.any(AbortSignal) },
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

describe("WorkflowCopilotChat — auto-accept Turn off across a chat switch", () => {
  it.each([
    { action: "Turn off", button: /Auto-accepting/ },
    { action: "Reject", button: "Reject" },
  ])(
    "leaves the new chat's auto-accept and pending review alone when the old chat's $action lands late",
    async ({ button }) => {
      await renderChat();
      await flushHistory(
        historyData({
          auto_accept: true,
          proposed_workflow: {
            workflow_id: "wf_pending",
            title: "Pending draft",
            _copilot_unvalidated: true,
          },
          chat_history: [aiHistoryMessage(null, "Here is a draft.")],
        }),
      );
      let finishRequest: (value: unknown) => void = () => {};
      cancelPost.mockImplementationOnce(
        () => new Promise((resolve) => (finishRequest = resolve)),
      );
      await act(async () => {
        fireEvent.click(await screen.findByRole("button", { name: button }));
      });

      await act(async () => {
        fireEvent.click(screen.getByText("mock-select-history-chat"));
        await Promise.resolve();
      });
      await flushHistory(
        historyData({
          workflow_copilot_chat_id: "chat_other",
          auto_accept: true,
          proposed_workflow: {
            workflow_id: "wf_other_pending",
            title: "Other chat draft",
            _copilot_unvalidated: true,
          },
          chat_history: [aiHistoryMessage(null, "Other chat draft.")],
        }),
      );
      expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
      await act(async () => {
        finishRequest({});
        await Promise.resolve();
      });

      expect(cancelPost).toHaveBeenCalledWith(
        expect.stringMatching(
          /\/workflow\/copilot\/(disable-auto-accept|clear-proposed-workflow)/,
        ),
        expect.objectContaining({ workflow_copilot_chat_id: "chat-1" }),
      );
      expect(
        screen.getByRole("button", { name: /Auto-accepting/ }),
      ).toBeTruthy();
      expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    },
  );

  it("keeps a chat's Accept withheld while its own Turn off is still pending, after another chat's Turn off starts", async () => {
    const pendingChat = (chatId: string) =>
      historyData({
        workflow_copilot_chat_id: chatId,
        auto_accept: true,
        proposed_workflow: {
          workflow_id: `wf_${chatId}`,
          title: "Pending draft",
          _copilot_unvalidated: true,
        },
        chat_history: [aiHistoryMessage(null, "Here is a draft.")],
      });
    const heldDisables: Array<(value: unknown) => void> = [];
    cancelPost.mockImplementation((path: string) =>
      path === "/workflow/copilot/disable-auto-accept"
        ? new Promise((resolve) => heldDisables.push(resolve))
        : Promise.resolve({}),
    );
    await renderChat();
    await flushHistory(pendingChat("chat-1"));
    await act(async () => {
      fireEvent.click(
        await screen.findByRole("button", { name: /Auto-accepting/ }),
      );
    });
    expect(screen.queryByRole("button", { name: "Always accept" })).toBeNull();

    // A second chat's Turn off starts while the first one is still in flight.
    await act(async () => {
      fireEvent.click(screen.getByText("mock-select-history-chat"));
      await Promise.resolve();
    });
    await flushHistory(pendingChat("chat_other"));
    await act(async () => {
      fireEvent.click(
        await screen.findByRole("button", { name: /Auto-accepting/ }),
      );
    });

    // Back to the first chat, whose disable has still not answered.
    await act(async () => {
      fireEvent.click(screen.getByText("mock-select-history-chat-1"));
      await Promise.resolve();
    });
    await flushHistory(pendingChat("chat-1"));

    expect(heldDisables).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Always accept" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
  });

  it("shows the chat's Turn off still pending after a round trip to another chat, and refuses a second one", async () => {
    const pendingChat = (chatId: string) =>
      historyData({
        workflow_copilot_chat_id: chatId,
        auto_accept: true,
        proposed_workflow: {
          workflow_id: `wf_${chatId}`,
          title: "Pending draft",
          _copilot_unvalidated: true,
        },
        chat_history: [aiHistoryMessage(null, "Here is a draft.")],
      });
    const heldDisables: Array<(value: unknown) => void> = [];
    cancelPost.mockImplementation((path: string) =>
      path === "/workflow/copilot/disable-auto-accept"
        ? new Promise((resolve) => heldDisables.push(resolve))
        : Promise.resolve({}),
    );
    await renderChat();
    await flushHistory(pendingChat("chat-1"));
    await act(async () => {
      fireEvent.click(
        await screen.findByRole("button", { name: /Auto-accepting/ }),
      );
    });
    expect(heldDisables).toHaveLength(1);

    // Away and back while chat-1's disable is still in flight: the chip remounts.
    await act(async () => {
      fireEvent.click(screen.getByText("mock-select-history-chat"));
      await Promise.resolve();
    });
    await flushHistory(pendingChat("chat_other"));
    await act(async () => {
      fireEvent.click(screen.getByText("mock-select-history-chat-1"));
      await Promise.resolve();
    });
    await flushHistory(pendingChat("chat-1"));

    const chip = await screen.findByRole("button", {
      name: /Auto-accepting/,
    });
    expect(chip.getAttribute("aria-disabled")).toBe("true");
    await act(async () => {
      fireEvent.click(chip);
    });
    expect(heldDisables).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Always accept" })).toBeNull();

    await act(async () => {
      heldDisables[0]!({});
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /Auto-accepting/ }),
      ).toBeNull(),
    );
    expect(screen.getByRole("button", { name: "Always accept" })).toBeTruthy();
  });

  it("loads another chat's proposal after the outstanding Accept settles", async () => {
    const pendingChat = (chatId: string) =>
      historyData({
        workflow_copilot_chat_id: chatId,
        auto_accept: false,
        proposed_workflow: {
          workflow_id: `wf_${chatId}`,
          title: "Pending draft",
          _copilot_unvalidated: true,
        },
        chat_history: [aiHistoryMessage(null, "Here is a draft.")],
      });
    let finishApply: (value: unknown) => void = () => {};
    cancelPost.mockImplementation((path: string) =>
      path === "/workflow/copilot/apply-proposed-workflow"
        ? new Promise((resolve) => (finishApply = resolve))
        : Promise.resolve({}),
    );
    await renderChat();
    await flushHistory(pendingChat("chat-1"));
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Accept" }));
    });

    await act(async () => {
      fireEvent.click(screen.getByText("mock-select-history-chat"));
      await Promise.resolve();
    });
    expect(historyQueue).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();

    await act(async () => {
      finishApply({ data: { workflow_id: "wf_chat-1" } });
    });

    await act(async () =>
      fireEvent.click(screen.getByText("mock-select-history-chat")),
    );
    await flushHistory(pendingChat("chat_other"));
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.getByText("Proposed changes")).toBeTruthy();
  });

  it("reconciles the accepted chat after refusing a history switch", async () => {
    const pendingChat = (chatId: string) =>
      historyData({
        workflow_copilot_chat_id: chatId,
        auto_accept: false,
        proposed_workflow: {
          workflow_id: `wf_${chatId}`,
          title: "Pending draft",
          _copilot_unvalidated: true,
        },
        chat_history: [aiHistoryMessage(null, "Here is a draft.")],
      });
    let failApply: (reason: unknown) => void = () => {};
    cancelPost.mockImplementation((path: string) =>
      path === "/workflow/copilot/apply-proposed-workflow"
        ? new Promise((_resolve, reject) => (failApply = reject))
        : Promise.resolve({}),
    );
    await renderChat();
    await flushHistory(pendingChat("chat-1"));
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Accept" }));
    });

    await act(async () => {
      fireEvent.click(screen.getByText("mock-select-history-chat"));
      await Promise.resolve();
    });
    expect(historyQueue).toHaveLength(0);

    const readsBeforeFailure = historyParams.length;
    await act(async () => {
      failApply({ response: { status: 422 } });
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(
        historyParams
          .slice(readsBeforeFailure)
          .map((params) => params?.workflow_copilot_chat_id),
      ).toContain("chat-1"),
    );
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
  });

  it("never clears a different chat through a failed Accept fallback", async () => {
    const pendingChat = (chatId: string) =>
      historyData({
        workflow_copilot_chat_id: chatId,
        auto_accept: false,
        proposed_workflow: {
          workflow_id: `wf_${chatId}`,
          title: "Pending draft",
          _copilot_unvalidated: true,
        },
        chat_history: [aiHistoryMessage(null, "Here is a draft.")],
      });
    let failApply: (reason: unknown) => void = () => {};
    const clearBodies: Array<Record<string, unknown>> = [];
    cancelPost.mockImplementation(
      (path: string, body: Record<string, unknown>) => {
        if (path === "/workflow/copilot/apply-proposed-workflow") {
          return new Promise((_resolve, reject) => (failApply = reject));
        }
        if (path === "/workflow/copilot/clear-proposed-workflow") {
          clearBodies.push(body);
          return Promise.reject({ response: { status: 404 } });
        }
        return Promise.resolve({});
      },
    );
    await renderChat();
    await flushHistory(pendingChat("chat-1"));
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Accept" }));
    });

    await act(async () => {
      fireEvent.click(screen.getByText("mock-select-history-chat"));
      await Promise.resolve();
    });
    expect(historyQueue).toHaveLength(0);

    await act(async () => {
      failApply({ response: { status: 422 } });
      await Promise.resolve();
    });
    await flushHistory(pendingChat("chat-1"));
    expect(clearBodies).toEqual([]);
    expect(
      screen.getByRole("button", { name: "Accept" }).matches(":disabled"),
    ).toBe(true);
  });

  it("blocks navigation to another chat's Turn off during Accept", async () => {
    await renderChat();
    await flushHistory(
      historyData({
        auto_accept: true,
        proposed_workflow: {
          workflow_id: "wf_pending",
          title: "Pending draft",
          _copilot_unvalidated: true,
        },
        chat_history: [aiHistoryMessage(null, "Here is a draft.")],
      }),
    );
    cancelPost.mockImplementation((path: string) =>
      path === "/workflow/copilot/apply-proposed-workflow"
        ? new Promise(() => {})
        : Promise.resolve({}),
    );
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Accept" }));
    });

    await act(async () => {
      fireEvent.click(screen.getByText("mock-select-history-chat"));
      await Promise.resolve();
    });
    expect(historyQueue).toHaveLength(0);
    expect(
      cancelPost.mock.calls.some(
        ([path]) => path === "/workflow/copilot/disable-auto-accept",
      ),
    ).toBe(false);
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
  });

  it("does not let a late post-Accept read of the old chat overwrite the chat the user switched to", async () => {
    await renderChat();
    await flushHistory(
      historyData({
        auto_accept: true,
        proposed_workflow: {
          workflow_id: "wf_pending",
          title: "Pending draft",
          _copilot_unvalidated: true,
        },
        chat_history: [aiHistoryMessage(null, "Here is a draft.")],
      }),
    );
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Accept" }));
    });
    // A plain Accept reads chat-1's row back; that read is still open when the user switches.
    await waitFor(() => expect(historyQueue.length).toBe(1));
    const lateChatOneRead = historyQueue.shift()!;
    historyRejects.shift();

    await act(async () => {
      fireEvent.click(screen.getByText("mock-select-history-chat"));
      await Promise.resolve();
    });
    await flushHistory(
      historyData({
        workflow_copilot_chat_id: "chat_other",
        auto_accept: true,
      }),
    );
    await act(async () => {
      lateChatOneRead({
        data: historyData({
          auto_accept: false,
          proposed_workflow: { workflow_id: "wf_chat_one", title: "Chat one" },
        }),
      });
      await Promise.resolve();
    });

    expect(screen.getByRole("button", { name: /Auto-accepting/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
  });
});

describe("WorkflowCopilotChat — startup and live-browser queue", () => {
  it("holds sending until startup settles, then keeps queued status and Cancel visible", async () => {
    boolFlags.current = {};
    await renderChat({ requiresLiveBrowser: true, isLiveBrowserReady: false });
    await submit("log into the portal");
    expect(screen.queryByTestId("copilot-queued-message")).toBeNull();
    expect(postStreaming).not.toHaveBeenCalled();
    await flushHistory(
      historyData({ chat_history: [aiHistoryMessage(null, "Earlier chat.")] }),
    );
    await submit("log into the portal");
    expect(screen.getByTestId("copilot-queued-message").textContent).toContain(
      "log into the portal",
    );
    await act(async () =>
      fireEvent.click(
        screen.getByRole("button", { name: "Edit queued message" }),
      ),
    );
    expect(screen.queryByTestId("copilot-queued-message")).toBeNull();
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
  function recoveredHistory(
    createdAt = "2025-01-01T00:00:00",
    terminalReason?: string,
  ): HistoryData {
    return historyWithRow({
      ...capturedAiRow,
      created_at: createdAt,
      turn_outcome: {
        ...capturedAiRow.turn_outcome,
        terminal_reason:
          terminalReason ?? capturedAiRow.turn_outcome!.terminal_reason,
      },
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
    props: Parameters<typeof chatUi>[0] = {},
  ): Promise<ReturnType<typeof render>> {
    const handle = await renderChat(props);
    await flushHistory(
      historyData({ workflow_copilot_chat_id: chatId, auto_accept: true }),
    );
    await submit("build me a flow");
    await waitFor(() => expect(streamCalls.length).toBe(1));
    await emitTurnStart();
    vi.useFakeTimers();
    return handle;
  }

  async function startBackgroundRecovery(): Promise<void> {
    await renderChat();
    await waitFor(() => expect(historyQueue.length).toBeGreaterThan(0));
    vi.useFakeTimers();
    await resolveNextHistory(
      historyData({
        chat_history: [
          {
            sender: "product",
            content:
              "Refine the recording (1 actions) into a reusable workflow.",
            turn_id: turnId,
            created_at: "2025-01-01T00:00:00",
          },
        ],
      }),
    );
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

  it("does not let a recovery read that started before Turn off turn the chip back on", async () => {
    await renderChat();
    await flushHistory(historyData({ auto_accept: true }));
    await submit("build me a flow");
    await waitFor(() => expect(streamCalls.length).toBe(1));
    await emitTurnStart();
    vi.useFakeTimers();
    await closeStreamWithoutTerminal();
    await advance(2_000);
    expect(historyQueue.length).toBe(1);
    const stalePoll = historyQueue.shift()!;
    historyRejects.shift();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Auto-accepting/ }));
    });
    await advance(0);
    expect(screen.queryByRole("button", { name: /Auto-accepting/ })).toBeNull();

    await act(async () => {
      stalePoll({ data: { ...recoveredHistory(), auto_accept: true } });
      await Promise.resolve();
    });

    expect(renderedText()).toContain(capturedAiText);
    expect(screen.queryByRole("button", { name: /Auto-accepting/ })).toBeNull();
  });

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

  it("reads past the reconcile threshold and waits for the interrupted row to be superseded", async () => {
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
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    await advance(30_000);
    await resolveNextHistory(recoveredHistory());
    expect(renderedText()).toContain(capturedAiText);
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
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
    await startBackgroundRecovery();

    await submit("another one");
    for (let i = 0; i < 20 && streamCalls.length < 1; i += 1) {
      await advance(10);
    }
    expect(streamCalls.length).toBe(1);
    await emitTerminalResponse();

    await advance(2_000);
    await resolveNextHistory(recoveredHistory());
    expect(renderedText()).toContain(capturedAiText);
  });

  it("claims ownership when recovery discovers a credential pause", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();
    await advance(2_000);
    await resolveNextHistory(
      historyData({
        pending_credential_requests: [
          {
            type: "credential_required",
            turn_id: turnId,
            workflow_copilot_chat_id: "chat-1",
            resume_token: "resume-token",
            reason: "workflow_credential_inputs_unbound",
            message: "",
            login_page_urls: ["https://example.com/login"],
            credential_refs: [],
            timeout_seconds: 300,
            expires_at: new Date(Date.now() + 300_000).toISOString(),
            timestamp: new Date().toISOString(),
          },
        ],
      }),
    );

    await submit("another one");
    await advance(10);

    expect(streamCalls.length).toBe(1);
  });

  it("does not re-read history when the stream ends on a terminal frame", async () => {
    await startTurn();
    await emitTerminalResponse();

    await advance(30_000);
    expect(historyQueue.length).toBe(0);
  });

  it.each(["resolved", "failed-post", "safety-timer"])(
    "retains recovery after an unconfirmed Stop (%s)",
    async (outcome) => {
      hasLocalChanges.current = true;
      const apply = vi.fn();
      await startTurn("chat-1", { onWorkflowUpdate: apply });
      const reservation =
        useWorkflowYamlEditorStore.getState().copilotAcceptance;
      if (outcome === "failed-post")
        cancelPost.mockRejectedValueOnce(new Error("offline"));
      await act(async () => {
        fireEvent.keyDown(document, { key: "Escape" });
        await Promise.resolve();
      });
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/cancel",
        expect.anything(),
        expect.objectContaining({
          timeout: 15_000,
          signal: expect.any(AbortSignal),
        }),
      );
      if (outcome === "safety-timer") await advance(15_000);
      await act(async () => streamCalls[0]!.resolve());
      expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
        reservation,
      );
      await advance(2_000);
      expect(historyQueue).toHaveLength(1);
      await resolveNextHistory(recoveredHistory());
      expect(workflowGets).toContain("/workflows/wpid_1");
      expect(apply).toHaveBeenCalledExactlyOnceWith(
        workflowResponse.current,
        expect.objectContaining({ persisted: true, applied: true }),
      );
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
    },
  );

  it("releases a confirmed Stop after terminal history confirms cancellation", async () => {
    workflowResponse.current = {
      workflow_id: "wf_1",
      workflow_permanent_id: "wpid_1",
    };
    const apply = vi.fn();
    await startTurn("chat-1", { onWorkflowUpdate: apply });
    const reservation = useWorkflowYamlEditorStore.getState().copilotAcceptance;
    await act(async () => fireEvent.keyDown(document, { key: "Escape" }));
    expect(cancelPost).toHaveBeenCalledWith(
      "/workflow/copilot/cancel",
      expect.anything(),
      expect.objectContaining({
        timeout: 15_000,
        signal: expect.any(AbortSignal),
      }),
    );
    await act(async () => streamCalls[0]!.resolve());
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
      reservation,
    );
    await advance(2_000);
    await resolveNextHistory(
      historyWithRow({
        ...capturedAiRow,
        content: "Cancelled.",
        turn_outcome: {
          ...capturedAiRow.turn_outcome,
          terminal_reason: "cancelled",
        },
      }),
    );
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    expect(apply).not.toHaveBeenCalled();
    await advance(30_000);
    expect(historyQueue).toHaveLength(0);
  });

  it("correlates an unannounced turn by request before releasing its reservation", async () => {
    await renderChat();
    await flushHistory(historyData());
    await submit("build me a flow");
    await waitFor(() => expect(streamCalls.length).toBe(1));
    vi.useFakeTimers();
    await closeStreamWithoutTerminal();

    await advance(2_000);
    expect(historyParams[historyParams.length - 1]).toEqual(
      expect.objectContaining({ request_cancel_token: expect.any(String) }),
    );
    await resolveNextHistory(recoveredHistory());
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    await advance(3_000);
    await resolveNextHistory({
      ...recoveredHistory(),
      request_turn_id: turnId,
    });
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
  });

  it("retains the reservation through read failures and after the budget expires", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();
    expect(renderedText()).toContain(
      "Copilot is checking whether this turn finished",
    );

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
    expect(historyQueue.length).toBe(1);
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    expect(renderedText()).toContain(
      "Copilot is checking whether this turn finished",
    );
    historyQueue.shift();
    const inFlight = historySignals[historySignals.length - 1];
    await advance(1_500_000);
    expect(inFlight?.aborted).toBe(true);
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    expect(historyQueue.length).toBe(1);
    expect(historySignals[historySignals.length - 1]?.aborted).toBe(true);
    expect(renderedText()).toContain(
      "Could not confirm whether Copilot saved changes",
    );
    expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
  });

  it("reconciles a reserved turn after the user leaves its chat mid-stream", async () => {
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

    await advance(2_000);
    expect(historyQueue).toHaveLength(1);
    expect(historyParams[historyParams.length - 1]).toEqual({
      workflow_copilot_chat_id: "chat-1",
    });
    await resolveNextHistory(recoveredHistory());
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    expect(renderedText()).not.toContain(capturedAiText);
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

  it("keeps reserved recovery when a chat switch lands during the connected-account refresh", async () => {
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

    await advance(2_000);
    expect(historyQueue).toHaveLength(1);
    await resolveNextHistory(recoveredHistory());
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    expect(renderedText()).not.toContain(capturedAiText);
  });

  it("renders an untagged reply but keeps the reservation until terminal confirmation", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();

    await advance(2_000);
    await resolveNextHistory(untaggedHistory());
    expect(renderedText()).toContain(capturedAiText);
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    expect(workflowGets).toHaveLength(1);

    await advance(3_000);
    await resolveNextHistory(
      recoveredHistory(new Date(Date.now() + 60_000).toISOString()),
    );
    expect(renderedText()).toContain(capturedAiText);
    expect(workflowGets).toHaveLength(2);
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
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
    expect(historyQueue.length).toBe(1);
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    expect(screen.getByRole("button", { name: "Reload" })).toBeTruthy();
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
    await startBackgroundRecovery();

    await advance(2_000);
    expect(historyQueue.length).toBe(1);
    const staleRead = historyQueue.shift()!;

    // A whole send begins and ends while that read is outstanding, so the
    // in-flight flag is false at both ends of it.
    await submit("another one");
    for (let i = 0; i < 20 && streamCalls.length < 1; i += 1) {
      await advance(10);
    }
    expect(streamCalls.length).toBe(1);
    await act(async () => {
      streamCalls[0]!.resolve();
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

  it("replaces the refreshing notice with required reconciliation when the budget runs out", async () => {
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
    expect(renderedText()).toContain(
      "Could not confirm whether Copilot saved changes",
    );
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
  });

  it("stops watching for a supersede well short of the budget", async () => {
    await startBackgroundRecovery();

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

  it("confirms uncertain persistence despite unsaved edits", async () => {
    hasLocalChanges.current = true;
    await startTurn();
    await closeStreamWithoutTerminal();

    // A failed transport must check whether the server persisted a new version.
    expect(workflowGets).toEqual(["/workflows/wpid_1"]);

    await advance(2_000);
    await resolveNextHistory(
      recoveredHistory("2025-01-01T00:00:00", "completed"),
    );
    expect(renderedText()).toContain(capturedAiText);

    await act(async () => {
      await Promise.resolve();
    });
    expect(workflowGets).toEqual(["/workflows/wpid_1", "/workflows/wpid_1"]);
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
  });

  it("confirms uncertain persistence for a normally finished turn, whose terminal_reason is null", async () => {
    hasLocalChanges.current = true;
    await startTurn();
    await closeStreamWithoutTerminal();

    expect(workflowGets).toEqual(["/workflows/wpid_1"]);

    await advance(2_000);
    await resolveNextHistory(recoveredHistory());
    expect(renderedText()).toContain(capturedAiText);

    await act(async () => {
      await Promise.resolve();
    });
    expect(workflowGets).toEqual(["/workflows/wpid_1", "/workflows/wpid_1"]);
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
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

  it("blocks a later send until the earlier turn's recovery finishes", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();

    await submit("another one");
    expect(streamCalls.length).toBe(1);
    await advance(2_000);
    await resolveNextHistory(recoveredHistory());
    expect(renderedText()).toContain(capturedAiText);

    await submit("another one");
    for (let i = 0; i < 20 && streamCalls.length < 2; i += 1) {
      await advance(10);
    }
    expect(streamCalls.length).toBe(2);
    await emitTerminalResponse(1);
  });

  it("restores a stopped turn only after terminal history and a canonical re-read", async () => {
    const snapshot: EditorStateSnapshot = {
      workflowPermanentId: "wpid_1",
      nodes: [],
      edges: [],
      parameters: [],
      title: "Unsaved title",
      titleHasBeenGenerated: false,
      description: "Unsaved description",
      hasChanges: true,
      saveGeneration: 0,
    };
    const restore = vi.fn().mockReturnValue("restored");
    const canonical = { workflow_id: "wf_1", workflow_permanent_id: "wpid_1" };
    workflowGet.mockResolvedValue({ data: canonical });
    await startTurn("chat-1", {
      captureEditorState: () => snapshot,
      restoreEditorState: restore,
    });
    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "workflow_draft",
        block_labels: [],
        workflow: canonical,
      });
      fireEvent.keyDown(document, { key: "Escape" });
      await Promise.resolve();
    });
    expect(cancelPost).toHaveBeenCalledWith(
      "/workflow/copilot/cancel",
      expect.anything(),
      expect.objectContaining({
        timeout: 15_000,
        signal: expect.any(AbortSignal),
      }),
    );
    await closeStreamWithoutTerminal();
    expect(workflowGets).toHaveLength(1);
    expect(restore).not.toHaveBeenCalled();

    await advance(2_000);
    await resolveNextHistory(historyData());
    expect(restore).not.toHaveBeenCalled();
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();

    let resolveCanonical!: (value: unknown) => void;
    workflowGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveCanonical = resolve;
        }),
    );
    await advance(3_000);
    const terminalHistory = recoveredHistory();
    (
      terminalHistory.chat_history[1] as { turn_outcome: unknown }
    ).turn_outcome = {
      copilot_turn_id: turnId,
      terminal_reason: "user_cancelled",
    };
    await resolveNextHistory(terminalHistory);
    expect(workflowGets).toHaveLength(2);
    expect(restore).not.toHaveBeenCalled();
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();

    await act(async () => resolveCanonical({ data: canonical }));
    expect(restore).toHaveBeenCalledExactlyOnceWith(snapshot);
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();

    await advance(1_500_000);
    expect(historyQueue.length).toBe(0);
    expect(workflowGets).toHaveLength(2);
  });

  it.each(["advanced", "unchanged", "timed-out"] as const)(
    "recovery Reject waits for the finalizer before resolving the %s canonical read",
    async (outcome) => {
      const snapshot: EditorStateSnapshot = {
        workflowPermanentId: "wpid_1",
        nodes: [],
        edges: [],
        parameters: [],
        title: "Unsaved title",
        titleHasBeenGenerated: false,
        description: "Unsaved description",
        hasChanges: true,
        saveGeneration: 0,
      };
      const canonical = {
        workflow_id: "wf_1",
        workflow_permanent_id: "wpid_1",
        workflow_definition: { blocks: [], parameters: [] },
      };
      const committed = { ...canonical, workflow_id: "wf_committed" };
      const restore = vi.fn().mockReturnValue("restored");
      const update = vi.fn();
      workflowGet.mockResolvedValue({ data: canonical });
      await renderChat({
        captureEditorState: () => snapshot,
        restoreEditorState: restore,
        onWorkflowUpdate: update,
      });
      await flushHistory(historyData({ auto_accept: true }));
      await submit("build me a flow");
      await waitFor(() => expect(streamCalls).toHaveLength(1));
      await emitTurnStart();
      vi.useFakeTimers();
      await act(async () => {
        streamCalls[0]!.onMessage({
          type: "workflow_draft",
          block_labels: [],
          workflow: canonical,
        });
        streamCalls[0]!.onMessage({
          type: "error",
          turn_id: turnId,
          error: "The stream ended while saving the workflow.",
        });
        streamCalls[0]!.resolve();
      });
      expect(workflowGets).toHaveLength(1);
      expect(restore).not.toHaveBeenCalled();
      update.mockClear();
      const reservation =
        useWorkflowYamlEditorStore.getState().copilotAcceptance;
      expect(reservation).not.toBeNull();

      let resolveCancel!: (value: unknown) => void;
      cancelPost.mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveCancel = resolve;
          }),
      );
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Reject" }));
      });
      expect(restore).not.toHaveBeenCalled();
      expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
        reservation,
      );
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/cancel",
        {
          cancel_token: (
            postStreaming.mock.calls[0]![1] as { cancel_token: string }
          ).cancel_token,
          workflow_copilot_chat_id: "chat-1",
          source: "stop_button",
        },
        { timeout: 5_000 },
      );
      expect(renderedText()).toContain("Cancelling the Copilot turn");
      expect(
        (screen.getByRole("button", { name: "Reject" }) as HTMLButtonElement)
          .disabled,
      ).toBe(false);
      expect(
        (
          screen.getByRole("button", {
            name: "Retry",
          }) as HTMLButtonElement
        ).disabled,
      ).toBe(false);

      await advance(2_000);
      await resolveNextHistory(historyData({ auto_accept: true }));
      expect(restore).not.toHaveBeenCalled();
      expect(update).not.toHaveBeenCalled();
      expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
        reservation,
      );
      await act(async () => resolveCancel({}));

      let resolveCanonical!: (value: unknown) => void;
      workflowGet.mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveCanonical = resolve;
          }),
      );
      await advance(3_000);
      await resolveNextHistory(recoveredHistory());
      expect(workflowGets).toHaveLength(2);
      expect(restore).not.toHaveBeenCalled();
      expect(update).not.toHaveBeenCalled();
      expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
        reservation,
      );

      if (outcome === "timed-out") {
        await advance(5_000);
        expect(restore).not.toHaveBeenCalled();
        expect(update).not.toHaveBeenCalled();
        expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
          reservation,
        );
        expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
        expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
        await act(async () => resolveCanonical({ data: committed }));
        expect(update).not.toHaveBeenCalled();
        expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
          reservation,
        );
        return;
      }
      await act(async () =>
        resolveCanonical({
          data: outcome === "advanced" ? committed : canonical,
        }),
      );
      if (outcome === "advanced") {
        expect(restore).not.toHaveBeenCalled();
        expect(update).toHaveBeenCalledExactlyOnceWith(
          committed,
          expect.objectContaining({ persisted: true, applied: true }),
        );
      } else {
        expect(restore).toHaveBeenCalledExactlyOnceWith(snapshot);
        expect(update).not.toHaveBeenCalled();
      }
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
      expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
      await advance(30_000);
      expect(historyQueue).toHaveLength(0);
      expect(workflowGets).toHaveLength(2);
    },
  );

  it.each([
    ["unchanged", "credential"],
    ["advanced", "credential"],
    ["unchanged", "question"],
    ["advanced", "question"],
  ] as const)(
    "Reject before turn_start retains recovery until the %s canonical outcome settles after a %s lookup",
    async (outcome, lookup) => {
      const snapshot: EditorStateSnapshot = {
        workflowPermanentId: "wpid_1",
        nodes: [],
        edges: [],
        parameters: [],
        title: "Unsaved title",
        titleHasBeenGenerated: false,
        description: "Unsaved description",
        hasChanges: true,
        saveGeneration: 0,
      };
      const canonical = {
        workflow_id: "wf_1",
        workflow_permanent_id: "wpid_1",
        workflow_definition: { blocks: [], parameters: [] },
      };
      const committed = { ...canonical, workflow_id: "wf_committed" };
      const restore = vi.fn().mockReturnValue("restored");
      const update = vi.fn();
      workflowGet.mockResolvedValue({ data: canonical });
      await renderChat({
        captureEditorState: () => snapshot,
        restoreEditorState: restore,
        onWorkflowUpdate: update,
      });
      await flushHistory(historyData());
      await submit("build me a flow");
      await waitFor(() => expect(streamCalls).toHaveLength(1));
      vi.useFakeTimers();
      await closeStreamWithoutTerminal();
      const reservation =
        useWorkflowYamlEditorStore.getState().copilotAcceptance;
      expect(reservation).not.toBeNull();
      const requestId = (
        postStreaming.mock.calls[0]![1] as { cancel_token: string }
      ).cancel_token;

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Reject" }));
      });
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/cancel",
        {
          cancel_token: requestId,
          workflow_copilot_chat_id: "chat-1",
          source: "stop_button",
        },
        { timeout: 5_000 },
      );
      expect(restore).not.toHaveBeenCalled();
      expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
        reservation,
      );
      expect(renderedText()).toContain("Cancelling the Copilot turn");

      await advance(2_000);
      await resolveNextHistory(otherTurnHistory());
      expect(restore).not.toHaveBeenCalled();
      expect(update).not.toHaveBeenCalled();
      expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
        reservation,
      );

      await advance(5_000);
      await resolveNextHistory(
        historyData(
          lookup === "question"
            ? {
                request_turn_id: turnId,
                pending_question_cancel_token: requestId,
                question_interactions: [
                  {
                    interaction_id: "question-1",
                    turn_id: turnId,
                    tool_call_id: "tool-1",
                    parts: [
                      {
                        part_id: "part-1",
                        prompt: "Choose the input",
                        choices: [],
                      },
                    ],
                    status: "pending",
                    response: null,
                    created_at: new Date().toISOString(),
                    resolved_at: null,
                  },
                ],
              }
            : {
                request_turn_id: turnId,
                pending_credential_requests: [
                  {
                    type: "credential_required",
                    turn_id: turnId,
                    workflow_copilot_chat_id: "chat-1",
                    resume_token: "resume-token",
                    reason: "workflow_credential_inputs_unbound",
                    message: "",
                    login_page_urls: ["https://example.com/login"],
                    credential_refs: [],
                    timeout_seconds: 300,
                    expires_at: new Date(Date.now() + 300_000).toISOString(),
                    timestamp: new Date().toISOString(),
                  },
                ],
              },
        ),
      );
      expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
        reservation,
      );

      let resolveCanonical!: (value: unknown) => void;
      workflowGet.mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveCanonical = resolve;
          }),
      );
      await advance(10_000);
      expect(historyQueue).toHaveLength(lookup === "question" ? 3 : 1);
      await resolveNextHistory(recoveredHistory());
      if (lookup === "question") {
        await resolveNextHistory(recoveredHistory());
        await resolveNextHistory(recoveredHistory());
      }
      expect(resolveCanonical).toBeTypeOf("function");
      expect(restore).not.toHaveBeenCalled();
      expect(update).not.toHaveBeenCalled();
      expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
        reservation,
      );

      await act(async () =>
        resolveCanonical({
          data: outcome === "advanced" ? committed : canonical,
        }),
      );
      if (outcome === "advanced") {
        expect(restore).not.toHaveBeenCalled();
        expect(update).toHaveBeenCalledExactlyOnceWith(
          committed,
          expect.objectContaining({ persisted: true, applied: true }),
        );
      } else {
        expect(restore).toHaveBeenCalledExactlyOnceWith(snapshot);
        expect(update).not.toHaveBeenCalled();
      }
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
      expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
      await advance(1_500_000);
      expect(historyQueue).toHaveLength(0);
    },
  );

  it("logs a 422 cancellation status and request id while retaining the recovery toast", async () => {
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      await startTurn();
      await closeStreamWithoutTerminal();
      log.mockClear();
      vi.mocked(toast).mockClear();
      cancelPost.mockRejectedValueOnce({ response: { status: 422 } });
      const reservation =
        useWorkflowYamlEditorStore.getState().copilotAcceptance;
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Reject" }));
      });
      expect(log).toHaveBeenCalledWith(
        "Failed to cancel the recovering Copilot turn:",
        {
          status: 422,
          requestId: (
            postStreaming.mock.calls[0]![1] as { cancel_token: string }
          ).cancel_token,
        },
      );
      expect(toast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Could not cancel the Copilot turn",
          description: "Copilot will keep checking for saved changes.",
        }),
      );
      expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
        reservation,
      );
      await advance(2_000);
      await resolveNextHistory(recoveredHistory());
      expect(
        useWorkflowYamlEditorStore.getState().copilotAcceptance,
      ).toBeNull();
    } finally {
      log.mockRestore();
    }
  });

  it("joins a late turn lookup to the original request recovery after remount", async () => {
    const canonical = {
      workflow_id: "wf_1",
      workflow_permanent_id: "wpid_1",
      workflow_definition: { blocks: [], parameters: [] },
    };
    workflowGet.mockResolvedValue({ data: canonical });
    const view = await renderChat();
    await flushHistory(historyData({ workflow_copilot_chat_id: null }));
    await submit("build me a flow");
    await waitFor(() => expect(streamCalls).toHaveLength(1));
    vi.useFakeTimers();
    await closeStreamWithoutTerminal();
    const requestId = (
      postStreaming.mock.calls[0]![1] as { cancel_token: string }
    ).cancel_token;
    view.unmount();
    const update = vi.fn();
    await act(async () => {
      render(chatUi({ onWorkflowUpdate: update }));
    });
    const paused = historyData({
      request_turn_id: turnId,
      pending_credential_requests: [
        {
          type: "credential_required",
          turn_id: turnId,
          workflow_copilot_chat_id: "chat-1",
          resume_token: "resume-token",
          reason: "workflow_credential_inputs_unbound",
          message: "",
          login_page_urls: ["https://example.com/login"],
          credential_refs: [],
          timeout_seconds: 300,
          expires_at: new Date(Date.now() + 300_000).toISOString(),
          timestamp: new Date().toISOString(),
        },
      ],
    });
    await resolveNextHistory({ ...paused, request_turn_id: null });
    const reservation = useWorkflowYamlEditorStore.getState().copilotAcceptance;
    expect(reservation).not.toBeNull();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    });
    expect(cancelPost).toHaveBeenCalledWith(
      "/workflow/copilot/cancel",
      {
        cancel_token: requestId,
        workflow_copilot_chat_id: null,
        source: "stop_button",
      },
      { timeout: 5_000 },
    );
    expect(historyQueue).toHaveLength(1);
    expect(historyParams[historyParams.length - 1]).toMatchObject({
      workflow_copilot_chat_id: "chat-1",
      request_cancel_token: requestId,
    });
    await resolveNextHistory(paused);
    const committed = { ...canonical, workflow_id: "wf_committed" };
    workflowGet.mockResolvedValue({ data: committed });
    await advance(2_000);
    expect(historyQueue).toHaveLength(1);
    await resolveNextHistory({
      ...recoveredHistory(),
      request_turn_id: turnId,
    });
    expect(update).toHaveBeenCalledExactlyOnceWith(
      committed,
      expect.objectContaining({ persisted: true, applied: true }),
    );
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    await advance(1_500_000);
    expect(historyQueue).toHaveLength(0);
  });

  async function recoverStagedTurn(
    remount: "mid-stream" | "after the drop" | "none",
    options: { terminalReason?: string; errorFrame?: boolean } = {},
  ): Promise<ReturnType<typeof vi.fn>> {
    const snapshot: EditorStateSnapshot = {
      workflowPermanentId: "wpid_1",
      nodes: [],
      edges: [],
      parameters: [],
      title: "Submitted title",
      titleHasBeenGenerated: false,
      description: "",
      hasChanges: false,
      saveGeneration: 0,
    };
    const restore = vi.fn().mockReturnValue("restored");
    const props = {
      captureEditorState: () => snapshot,
      restoreEditorState: restore,
    };
    workflowResponse.current = {
      workflow_id: "wf_1",
      workflow_permanent_id: "wpid_1",
    };
    const staged = {
      workflow_id: "wf_proposed",
      workflow_permanent_id: "wpid_1",
      workflow_definition: { blocks: [], parameters: [] },
    };
    const view = await renderChat(props);
    await flushHistory(historyData());
    await submit("build me a flow");
    await waitFor(() => expect(streamCalls).toHaveLength(1));
    await emitTurnStart();
    await act(async () => {
      streamCalls[0]!.onMessage({
        type: "workflow_draft",
        block_labels: [],
        workflow: staged,
      });
      if (options.errorFrame) {
        streamCalls[0]!.onMessage({
          type: "error",
          turn_id: turnId,
          error: "The stream ended while saving the workflow.",
        });
        streamCalls[0]!.resolve();
      }
      await Promise.resolve();
    });
    vi.useFakeTimers();
    if (remount !== "mid-stream" && !options.errorFrame)
      await closeStreamWithoutTerminal();
    if (remount !== "none") {
      view.unmount();
      await act(async () => {
        render(chatUi(props));
      });
      await resolveNextHistory({
        ...historyData(),
        chat_history: capturedHistory.chat_history.slice(0, -1),
      });
    }
    await advance(2_000);
    await resolveNextHistory({
      ...recoveredHistory(undefined, options.terminalReason),
      request_turn_id: turnId,
      proposed_workflow: staged,
      proposed_workflow_metadata: {
        owner_turn_id: turnId,
        revision: 1,
        canonical_fingerprint: "canonical-1",
        disposition: "review_untested",
        workflow_run_id: null,
      },
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    return restore;
  }

  it.each([
    ["mid-stream", null],
    ["after the drop", null],
    ["none", null],
    ["none", "verified_goal_satisfied"],
    ["mid-stream", "verified_goal_satisfied"],
    ["none", "timeout"],
    ["mid-stream", "timeout"],
    ["none", "turn_halt:browser_session_lost"],
    ["mid-stream", "turn_halt:browser_session_lost"],
  ] as const)(
    "keeps a recovered turn's staged proposal and canvas (remount: %s, terminal_reason: %s)",
    async (remount, terminalReason) => {
      const restore = await recoverStagedTurn(remount, {
        terminalReason: terminalReason ?? undefined,
      });

      expect(restore).not.toHaveBeenCalled();
      const accept = screen.getByRole("button", { name: "Accept" });
      expect(accept.hasAttribute("disabled")).toBe(false);
    },
  );

  it.each(["cancelled", "copilot_recoverable_failure", "user_cancelled"])(
    "still rolls back a turn recovered after a remount whose terminal_reason is %s",
    async (terminalReason) => {
      const restore = await recoverStagedTurn("mid-stream", { terminalReason });

      expect(restore).toHaveBeenCalledOnce();
    },
  );

  it("still rolls back a staged turn whose error frame arrived before the pane remounted", async () => {
    const restore = await recoverStagedTurn("mid-stream", { errorFrame: true });

    expect(restore).toHaveBeenCalledOnce();
  });

  it("keeps Reject reserved when a pre-cancellation canonical read returns before the turn is identified", async () => {
    const canonical = {
      workflow_id: "wf_1",
      workflow_permanent_id: "wpid_1",
      workflow_definition: { blocks: [], parameters: [] },
    };
    let resolveCanonical!: (value: unknown) => void;
    workflowGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveCanonical = resolve;
        }),
    );
    const update = vi.fn();
    const restore = vi.fn().mockReturnValue("restored");
    await renderChat({ onWorkflowUpdate: update, restoreEditorState: restore });
    await flushHistory(historyData());
    await submit("build me a flow");
    await waitFor(() => expect(streamCalls).toHaveLength(1));
    vi.useFakeTimers();
    await closeStreamWithoutTerminal();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    });
    const reservation = useWorkflowYamlEditorStore.getState().copilotAcceptance;
    expect(reservation).not.toBeNull();
    await act(async () =>
      resolveCanonical({ data: { ...canonical, workflow_id: "wf_advanced" } }),
    );
    expect(update).not.toHaveBeenCalled();
    expect(restore).not.toHaveBeenCalled();
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
      reservation,
    );
    await advance(2_000);
    await resolveNextHistory(recoveredHistory());
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
      reservation,
    );
    await advance(1_500_000);
    expect(renderedText()).toContain("Cancelling the Copilot turn");
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(
      reservation,
    );
    const reads = historyParams.length;
    await advance(30_000);
    expect(historyParams).toHaveLength(reads);
  });

  it("correlates an unannounced stream before applying its terminal history", async () => {
    await renderChat();
    await flushHistory(historyData());
    await submit("build me a flow");
    await waitFor(() => expect(streamCalls.length).toBe(1));
    vi.useFakeTimers();
    await closeStreamWithoutTerminal();

    await advance(30_000);
    expect(historyQueue.length).toBe(1);
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
  });

  it("keeps uncertain recovery reserved through read failures and timeout", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();
    expect(renderedText()).toContain(
      "Copilot is checking whether this turn finished",
    );

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
    expect(historyQueue.length).toBe(1);
    expect(renderedText()).toContain(
      "Copilot is checking whether this turn finished",
    );
    await advance(1_500_000);
    expect(historySignals[historySignals.length - 1]?.aborted).toBe(true);
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
    await submit("another one");
    expect(streamCalls.length).toBe(1);
  });

  it("continues the reserved history read after leaving the chat", async () => {
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
    expect(historyQueue.length).toBe(1);
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
  });

  it.each([false, true])(
    "arms the poll after the connected-account refresh (terminal before refresh=%s)",
    async (terminalBeforeRefresh) => {
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

      const refresh = historyQueue.shift()!;
      if (terminalBeforeRefresh) {
        await advance(2_000);
        await resolveNextHistory(recoveredHistory());
        expect(renderedText()).not.toContain(capturedAiText);
      }
      await act(async () =>
        refresh({ data: historyData({ chat_history: [] }) }),
      );

      await advance(3_000);
      await resolveNextHistory(recoveredHistory());
      expect(renderedText()).toContain(capturedAiText);
    },
  );

  it("keeps the original recovery poll when a chat switch lands during the connected-account refresh", async () => {
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
    expect(historyQueue.length).toBe(1);
    expect(historyParams[historyParams.length - 1]).toEqual({
      workflow_copilot_chat_id: "chat-1",
    });
    await resolveNextHistory(recoveredHistory());
    expect(renderedText()).not.toContain(capturedAiText);
    expect(screen.queryByRole("alert")).toBeNull();
    await advance(30_000);
    expect(historyQueue.length).toBe(0);
  });

  it("refuses another send while the recovery history read is outstanding", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();

    await advance(2_000);
    expect(historyQueue.length).toBe(1);
    const staleRead = historyQueue.shift()!;

    await submit("another one");
    expect(streamCalls.length).toBe(1);

    await act(async () => {
      staleRead({ data: recoveredHistory() });
      await Promise.resolve();
    });
    expect(renderedText()).toContain(capturedAiText);
    await submit("another one");
    expect(streamCalls.length).toBe(2);
    await emitTerminalResponse(1);
    expect(renderedText()).toContain("another one");

    await advance(30_000);
    expect(historyQueue.length).toBe(0);
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
    expect(renderedText()).toContain(
      "Could not confirm whether Copilot saved changes",
    );
  });

  it("stops uncertain supersede reads but keeps Retry and Reject reserved", async () => {
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
    expect(reads).toBe(30);
    expect(historyQueue.length).toBe(0);
    expect(workflowGets).toHaveLength(1);
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
    await submit("another one");
    expect(streamCalls).toHaveLength(1);
    await advance(1_500_000);
    while (historyQueue.length) await resolveNextHistory(interruptedHistory());
    expect(historyQueue).toHaveLength(0);

    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Retry" })),
    );
    expect(
      useWorkflowYamlEditorStore.getState().copilotAcceptance,
    ).not.toBeNull();
    await advance(2_000);
    await resolveNextHistory(recoveredHistory());
    expect(renderedText()).toContain(capturedAiText);
    expect(workflowGets).toHaveLength(3);
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    await advance(30_000);
    expect(historyQueue).toHaveLength(0);
  });

  it("rechecks canonical after the terminal row despite unsaved edits", async () => {
    hasLocalChanges.current = true;
    await startTurn();
    await closeStreamWithoutTerminal();

    // A failed transport must check whether the server persisted a new version.
    expect(workflowGets).toEqual(["/workflows/wpid_1"]);

    await advance(2_000);
    expect(workflowGets).toHaveLength(1);
    await resolveNextHistory(recoveredHistory());
    expect(renderedText()).toContain(capturedAiText);

    await act(async () => {
      await Promise.resolve();
    });
    expect(workflowGets).toEqual(["/workflows/wpid_1", "/workflows/wpid_1"]);
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBeNull();
    await advance(30_000);
    expect(workflowGets).toHaveLength(2);
    expect(historyQueue).toHaveLength(0);
  });

  it("retrying the same request keeps one recovery ladder", async () => {
    await startTurn();
    await closeStreamWithoutTerminal();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    });

    await advance(2_500);
    expect(historyQueue.length).toBe(1);
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

  it("clears loading after recovery Reject confirms the answered question was cancelled", async () => {
    const interaction: QuestionInteraction = {
      interaction_id: "interaction",
      turn_id: "turn-ask",
      tool_call_id: "call",
      status: "pending",
      response: null,
      created_at: "2026-09-04T00:00:00Z",
      resolved_at: null,
      parts: [{ part_id: "part", prompt: "Which store?", choices: [] }],
    };
    await renderChat();
    await flushHistory(
      historyData({
        question_interactions: [interaction],
        pending_question_cancel_token: "cancel-question",
      }),
    );
    let resolveAnswer!: (value: unknown) => void;
    let resolveCancel!: (value: unknown) => void;
    cancelPost
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveAnswer = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveCancel = resolve;
          }),
      );
    const composer = screen.getByPlaceholderText("Answer Copilot…");
    fireEvent.change(composer, { target: { value: "The test store" } });
    await act(async () => fireEvent.keyDown(composer, { key: "Enter" }));
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Reject" })),
    );
    expect(cancelPost.mock.calls.map(([path]) => path)).toEqual([
      "/workflow/copilot/question-response",
      "/workflow/copilot/cancel",
    ]);
    await act(async () =>
      resolveAnswer({
        data: {
          ...interaction,
          status: "resolved",
          response: { text: "The test store" },
        },
      }),
    );
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
    await act(async () => resolveCancel({}));
    await flushHistory(
      historyData({
        chat_history: [
          {
            sender: "ai",
            content: "Cancelled",
            created_at: new Date().toISOString(),
            turn_outcome: {
              copilot_turn_id: "turn-ask",
              terminal_reason: "cancelled",
            },
          },
        ],
      }),
    );

    expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
    await submit("build after cancellation");
    await waitFor(() => expect(streamCalls).toHaveLength(1));
  });
});
