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
import { COPILOT_UX_V1_FLAG } from "@/util/featureFlags";

type HistoryData = {
  workflow_copilot_chat_id: string | null;
  chat_history: unknown[];
  proposed_workflow: Record<string, unknown> | null;
  auto_accept: boolean;
};

// Only chat-history GETs are deferred (held here so a test controls
// isLoadingHistory); every other GET resolves immediately.
const { postStreaming, cancelPost, historyQueue, flagMap, boolFlags } =
  vi.hoisted(() => ({
    postStreaming: vi.fn(() => new Promise<void>(() => {})),
    cancelPost: vi.fn().mockResolvedValue({}),
    historyQueue: [] as Array<(resp: { data: HistoryData }) => void>,
    flagMap: { current: {} as Record<string, boolean> },
    boolFlags: { current: {} as Record<string, boolean> },
  }));

vi.mock("@/api/sse", () => ({
  getSseClient: vi.fn().mockResolvedValue({ postStreaming }),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn().mockResolvedValue({
    get: vi.fn((path: string) => {
      if (path === "/workflow/copilot/chat-history") {
        return new Promise((resolve) => {
          historyQueue.push(resolve as (resp: { data: HistoryData }) => void);
        });
      }
      return Promise.resolve({ data: [] });
    }),
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

vi.mock("posthog-js/react", () => ({
  useFeatureFlagEnabled: (flag: string) => flagMap.current[flag] ?? false,
}));

vi.mock("@/store/WorkflowHasChangesStore", () => ({
  useWorkflowHasChangesStore: () => ({
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
  }),
}));

vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: undefined }),
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

import { WorkflowCopilotChat } from "./WorkflowCopilotChat";

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
      <FeatureFlagValueContext.Provider value={() => undefined}>
        <WorkflowCopilotChat
          docked={props.docked ?? false}
          portalTarget={props.portalTarget}
          requiresLiveBrowser={props.requiresLiveBrowser}
          isLiveBrowserReady={props.isLiveBrowserReady}
        />
      </FeatureFlagValueContext.Provider>
    </FeatureFlagContext.Provider>
  );
}

async function renderChat(
  props: Parameters<typeof chatUi>[0] = {},
): Promise<void> {
  let portalTarget: HTMLElement | undefined;
  if (props.docked) {
    portalTarget = document.createElement("div");
    document.body.appendChild(portalTarget);
    portalTargets.push(portalTarget);
  }
  render(chatUi({ ...props, portalTarget }));
  await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
}

async function flushHistory(data: HistoryData): Promise<void> {
  await waitFor(() => expect(historyQueue.length).toBeGreaterThan(0));
  const resolve = historyQueue.shift()!;
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
  postStreaming.mockClear();
  cancelPost.mockClear();
  cancelPost.mockResolvedValue({});
  historyQueue.length = 0;
  flagMap.current = {};
  boolFlags.current = {};
});

afterEach(() => {
  cleanup();
  portalTargets.splice(0).forEach((el) => el.remove());
});

// Item 1 (SKY-12384): during a chat-history SWITCH, a prior chat's action card
// must not stay actionable — its action would post into the OUTGOING chat.
describe("WorkflowCopilotChat — history-race action-card gating (item 1)", () => {
  it("hides the review gate's Accept while a chat switch is loading (flag-on)", async () => {
    flagMap.current = { [COPILOT_UX_V1_FLAG]: true };
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

  it("hides the Confirm chip while a chat switch is loading (flag-on)", async () => {
    flagMap.current = { [COPILOT_UX_V1_FLAG]: true };
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

  it("hides the docked FixCard while a chat switch is loading (flag-off)", async () => {
    await renderChat({ docked: true });
    await flushHistory(
      historyData({
        chat_history: [
          aiHistoryMessage(
            narrativePayload({
              terminal: "error",
              terminalMessage: "The last run hit an error.",
              narrativeSummary: "The last run hit an error.",
            }),
            "The last run hit an error.",
          ),
        ],
      }),
    );

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Fix with Copilot" }),
      ).toBeTruthy(),
    );

    fireEvent.click(screen.getByText("mock-select-history-chat"));

    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Fix with Copilot" }),
      ).toBeNull(),
    );

    await flushHistory(historyData({ workflow_copilot_chat_id: "chat_other" }));
  });
});

// Item 2 (SKY-12384): a live_browser prompt queued before the initial history
// load must keep one owner — footer while the bubble exists, else the chip.
describe("WorkflowCopilotChat — queued prompt survives initial history load (item 2)", () => {
  it("keeps the queued status + Cancel visible after the history load lands", async () => {
    flagMap.current = { [COPILOT_UX_V1_FLAG]: true };
    boolFlags.current = { ENABLE_WORKFLOW_COPILOT_V2: true };
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
      screen.getByRole("button", { name: "Cancel queued message" }),
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
      name: "Cancel queued message",
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
