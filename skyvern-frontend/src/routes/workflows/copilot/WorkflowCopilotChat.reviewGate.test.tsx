import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import bundles from "./narrativeState.turnFacts.fixture.json";
import type { WorkflowCopilotStreamResponseUpdate } from "./workflowCopilotTypes";

const editOneOfTwoBundle = bundles["different-source-edit-one-of-two"];

type StreamBody = {
  message: string;
  keep_pending_proposal?: boolean;
};
type StreamCall = {
  body: StreamBody;
  onMessage: (payload: unknown) => boolean;
  resolve: () => void;
  reject: (error: unknown) => void;
};

const { streamCalls, postStreaming, cancelPost, historyGet, historyResponse } =
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
        workflow_copilot_chat_id: "chat-1" as string | null,
        chat_history: [] as unknown[],
        proposed_workflow: null as Record<string, unknown> | null,
        auto_accept: false,
      },
    };
    const get = vi.fn().mockImplementation(() => Promise.resolve(history));
    return {
      streamCalls: calls,
      postStreaming: streaming,
      cancelPost: post,
      historyGet: get,
      historyResponse: history,
    };
  });

vi.mock("@/api/sse", () => ({
  getSseClient: vi.fn().mockResolvedValue({ postStreaming }),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn().mockResolvedValue({
    get: historyGet,
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

vi.mock("@/store/WorkflowHasChangesStore", () => ({
  useWorkflowHasChangesStore: () => ({ getSaveData: () => saveData }),
}));

// Unrelated to this file's tests; the real hook needs a QueryClientProvider
// this harness doesn't set up.
vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: undefined }),
}));

import { WorkflowCopilotChat } from "./WorkflowCopilotChat";

async function renderChat(props: { docked?: boolean } = {}) {
  // docked renders via a portal; without a target it intentionally renders null.
  const portalTarget = props.docked ? document.body : undefined;
  const view = render(
    <WorkflowCopilotChat
      docked={props.docked ?? false}
      portalTarget={portalTarget}
    />,
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

const proposedWorkflowPayload = (
  overrides: Record<string, unknown> = {},
): Record<string, unknown> => ({
  workflow_id: "wf_proposed",
  title: "Draft workflow",
  _copilot_unvalidated: true,
  ...overrides,
});

const proposalNarrativePayload = (
  overrides: Record<string, unknown> = {},
): Record<string, unknown> => ({
  turnId: "turn-1",
  turnIndex: 0,
  mode: "build",
  responseType: "REPLY",
  cancelled: false,
  proposalDisposition: "review_untested",
  designStarted: true,
  designEnded: true,
  draft: {
    blockCount: 1,
    blockLabels: ["extract_titles"],
    summary: null,
  },
  blocks: [
    {
      workflowRunBlockId: "wrb_extract_titles",
      label: "extract_titles",
      blockType: "task",
      state: "completed",
      lastSeenIteration: 0,
      activity: [],
      startedAt: null,
      endedAt: null,
    },
  ],
  terminal: "response",
  terminalMessage: "Here is a draft workflow for you to review.",
  narrativeSummary: "Here is a draft workflow for you to review.",
  priorBlockCount: 0,
  designActivity: [],
  startedAt: "2026-07-09T00:00:00Z",
  endedAt: "2026-07-09T00:00:05Z",
  ...overrides,
});

const proposalResponse = (
  message: string,
  overrides: Partial<WorkflowCopilotStreamResponseUpdate> &
    Record<string, unknown> = {},
): WorkflowCopilotStreamResponseUpdate =>
  ({
    type: "response",
    workflow_copilot_chat_id: "chat-1",
    message,
    updated_workflow: proposedWorkflowPayload(),
    response_time: "2026-07-09T00:00:05Z",
    proposal_disposition: "review_untested",
    turn_id: "turn-1",
    narrative_payload: proposalNarrativePayload(),
    ...overrides,
  }) as WorkflowCopilotStreamResponseUpdate;

// The v1 (Ask-mode) backend streams no narrative frames at all: no turn_start,
// and a terminal frame carrying only the schema defaults for turn_id and
// narrative_payload. It never stamps _copilot_unvalidated either, so its
// proposal reads Tested.
const legacyProposalResponse = (
  message: string,
  overrides: Partial<WorkflowCopilotStreamResponseUpdate> &
    Record<string, unknown> = {},
): WorkflowCopilotStreamResponseUpdate =>
  ({
    type: "response",
    workflow_copilot_chat_id: "chat-1",
    message,
    updated_workflow: {
      workflow_id: "wf_proposed",
      title: "Draft workflow",
    },
    response_time: "2026-07-09T00:00:05Z",
    total_tokens: null,
    response_type: "REPLY",
    proposal_disposition: "auto_applicable",
    workflow_applied: false,
    cancelled: false,
    output_policy_diagnostics: null,
    turn_id: null,
    narrative_summary: null,
    narrative_payload: null,
    ...overrides,
  }) as WorkflowCopilotStreamResponseUpdate;

const plainReplyResponse = (
  message: string,
  overrides: Partial<WorkflowCopilotStreamResponseUpdate> &
    Record<string, unknown> = {},
): WorkflowCopilotStreamResponseUpdate =>
  ({
    type: "response",
    workflow_copilot_chat_id: "chat-1",
    message,
    updated_workflow: null,
    response_time: "2026-07-09T00:00:10Z",
    proposal_disposition: "no_proposal",
    turn_id: "turn-2",
    narrative_payload: {
      ...proposalNarrativePayload(),
      turnId: "turn-2",
      turnIndex: 1,
      draft: null,
      proposalDisposition: "no_proposal",
      terminalMessage: message,
      narrativeSummary: message,
    },
    ...overrides,
  }) as WorkflowCopilotStreamResponseUpdate;

beforeEach(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  streamCalls.length = 0;
  postStreaming.mockClear();
  cancelPost.mockClear();
  cancelPost.mockResolvedValue({});
  historyGet.mockClear();
  historyResponse.data = {
    workflow_copilot_chat_id: "chat-1",
    chat_history: [],
    proposed_workflow: null,
    auto_accept: false,
  };
});

afterEach(() => {
  cleanup();
});

describe("WorkflowCopilotChat — g2 review gate", () => {
  it("does not send keep_pending_proposal on a chat's first message (nothing pending yet)", async () => {
    await renderChat();

    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    expect(streamCalls[0]!.body.keep_pending_proposal).toBe(false);
  });

  it("restores an actionable gate via the chip after a bypassed proposal (old code: buttons vanish forever)", async () => {
    await renderChat();

    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Always accept" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Review" })).toBeTruthy();

    // Bypass: send a follow-up instead of acting on the gate.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    await submit("also grab the story scores");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));

    // keep_pending_proposal must ride along on the bypassing request.
    expect(streamCalls[1]!.body.keep_pending_proposal).toBe(true);
    // Mid-flight: the gate's own actions are not accessible while loading.
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(screen.getByText("1 proposal pending · Review")).toBeTruthy();

    // Turn 2 ends with no new proposal; resync picks the row back up.
    await act(async () => {
      streamCalls[1]!.onMessage(
        plainReplyResponse("I'll fold that into the draft above."),
      );
      streamCalls[1]!.resolve();
    });
    await waitFor(() => expect(historyGet).toHaveBeenCalled());

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy(),
    );
    // The gate's owning turn is still not the last message, so the chip
    // (and its jump-back affordance) stays up even once actions re-enable.
    const chip = screen.getByText("1 proposal pending · Review");
    await act(async () => {
      fireEvent.click(chip);
    });
    expect(HTMLElement.prototype.scrollIntoView).toHaveBeenCalled();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    await waitFor(() =>
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/apply-proposed-workflow",
        expect.objectContaining({ workflow_copilot_chat_id: "chat-1" }),
      ),
    );
  });

  it("shows the Untested pill on hydration of an old payload lacking proposalDisposition", async () => {
    historyResponse.data.proposed_workflow = proposedWorkflowPayload({
      _copilot_unvalidated: true,
    });
    historyResponse.data.chat_history = [
      {
        sender: "ai",
        content: "Here is a draft.",
        created_at: "2026-07-09T00:00:05Z",
        narrative_payload: null,
        turn_outcome: null,
      },
    ];
    await renderChat();

    await waitFor(() => expect(screen.getByText("Untested")).toBeTruthy());
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
  });

  it("renders a legacy (no-narrative) pending turn as pill + footer, no changes body", async () => {
    historyResponse.data.proposed_workflow = proposedWorkflowPayload({
      _copilot_unvalidated: true,
    });
    historyResponse.data.chat_history = [
      {
        sender: "ai",
        content: "Here is a draft.",
        created_at: "2026-07-09T00:00:05Z",
        narrative_payload: null,
        turn_outcome: null,
      },
    ];
    await renderChat();

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy(),
    );
    expect(screen.getByText("Proposed changes")).toBeTruthy();
    expect(screen.queryByText("Added")).toBeNull();
    expect(screen.queryByText("Removed")).toBeNull();
    // No narrative turn id to derive an owning turn from — the chip would be
    // a dead no-op affordance here, not a working jump-back.
    expect(screen.queryByText("1 proposal pending · Review")).toBeNull();
  });

  it("clears a stale pending gate when a later turn is auto-applied (stale Accept can't reapply over the newer canvas)", async () => {
    await renderChat();

    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();

    // Always-accept echo: this follow-up comes back auto-applied instead of another gate.
    await submit("actually just fix the typo directly");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    expect(streamCalls[1]!.body.keep_pending_proposal).toBe(true);

    await act(async () => {
      streamCalls[1]!.onMessage(
        proposalResponse("Fixed and applied.", {
          proposal_disposition: "auto_applicable",
          workflow_applied: true,
          turn_id: "turn-2",
          narrative_payload: proposalNarrativePayload({
            turnId: "turn-2",
            turnIndex: 1,
            proposalDisposition: "auto_applicable",
            terminalMessage: "Fixed and applied.",
            narrativeSummary: "Fixed and applied.",
          }),
        }),
      );
      streamCalls[1]!.resolve();
    });

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Accept" })).toBeNull(),
    );
    expect(screen.queryByText("1 proposal pending · Review")).toBeNull();
  });

  it("shows the gate live on a narrative-less turn, not only after a reload (SKY-14099)", async () => {
    await renderChat();

    await submit("rename the blocks.");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(legacyProposalResponse("Renamed the blocks."));
      streamCalls[0]!.resolve();
    });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy(),
    );
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
    expect(screen.getByText("Proposed changes")).toBeTruthy();
    // The gate itself still appears live (SKY-14099); with no turn behind it there are
    // no facts to project, so it carries no verdict pill either way.
    expect(screen.queryByText("Tested")).toBeNull();
  });

  it("clears the proposal and shows a discarded receipt on a late Reject", async () => {
    await renderChat();

    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy(),
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    });

    await waitFor(() =>
      expect(cancelPost).toHaveBeenCalledWith(
        "/workflow/copilot/clear-proposed-workflow",
        expect.objectContaining({ workflow_copilot_chat_id: "chat-1" }),
      ),
    );
    expect(
      screen.getByText("Discarded — canvas reverted to the previous version"),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
  });

  it("surfaces per-block coverage tags through the mounted chat, not only in an isolated card", async () => {
    await renderChat();

    await submit("edit one block after it passed.");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Draft needs review.", {
          narrative_payload: {
            ...editOneOfTwoBundle,
            turnId: "turn-1",
          },
        }),
      );
      streamCalls[0]!.resolve();
    });

    await waitFor(() =>
      expect(screen.getByText("Different source")).toBeTruthy(),
    );
    expect(screen.queryByRole("button", { name: "Collapse turn" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Draft needs review/ }),
    ).toBeNull();
    expect(screen.queryByText("Built and tested the workflow")).toBeNull();
  });

  it("keeps the newest structured response in its expanded reading state", async () => {
    await renderChat();

    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    await waitFor(() =>
      expect(
        screen.getByText("Here is a draft workflow for you to review."),
      ).toBeTruthy(),
    );
    expect(screen.queryByRole("button", { name: "Collapse turn" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Draft needs review/ }),
    ).toBeNull();
  });
});
