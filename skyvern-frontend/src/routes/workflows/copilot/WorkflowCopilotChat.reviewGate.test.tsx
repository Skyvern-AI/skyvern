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
        proposed_workflow_metadata: null as {
          owner_turn_id: string;
          revision: number;
          canonical_fingerprint: string;
          disposition: "review_untested";
          workflow_run_id: string | null;
        } | null,
        proposed_workflow_run: null as {
          workflow_run_id: string;
          status: string | null;
          available: boolean;
          failure_reason: string | null;
          outputs: Array<{ output_parameter_id: string; value: unknown }>;
        } | null,
        auto_accept: false as boolean,
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

import { toast } from "@/components/ui/use-toast";

import {
  ACCEPT_SETTLE_CEILING_MS,
  WorkflowCopilotChat,
} from "./WorkflowCopilotChat";

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
    proposed_workflow_metadata: null,
    proposed_workflow_run: null,
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

  it("interrupted_draft resyncs a stale typed Accept without applying it locally", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Draft ready.", {
          proposed_workflow_metadata: {
            owner_turn_id: "turn-1",
            revision: 1,
            canonical_fingerprint: "canonical-1",
            disposition: "review_untested",
            workflow_run_id: null,
          },
        }),
      );
      streamCalls[0]!.resolve();
    });

    historyResponse.data.proposed_workflow = proposedWorkflowPayload({
      title: "Newer draft",
    });
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-2",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: null,
    };
    cancelPost.mockRejectedValueOnce({ response: { status: 409 } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    await waitFor(() => expect(historyGet).toHaveBeenCalled());
    expect(cancelPost).toHaveBeenCalledWith(
      "/workflow/copilot/apply-proposed-workflow",
      expect.objectContaining({ owner_turn_id: "turn-1", revision: 1 }),
    );
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
  });

  it("retains and resyncs a typed proposal when atomic Accept fails", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Draft ready.", {
          proposed_workflow_metadata: {
            owner_turn_id: "turn-1",
            revision: 1,
            canonical_fingerprint: "canonical-1",
            disposition: "review_untested",
            workflow_run_id: null,
          },
        }),
      );
      streamCalls[0]!.resolve();
    });
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: null,
    };
    cancelPost.mockRejectedValueOnce({ response: { status: 500 } });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    await waitFor(() => expect(historyGet).toHaveBeenCalled());
    expect(cancelPost).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
  });

  it("does not locally accept a typed proposal when its chat cannot be resolved", async () => {
    historyResponse.data.workflow_copilot_chat_id = null;
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: null,
    };
    await renderChat();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    expect(cancelPost).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
  });

  it("interrupted_draft reload shows an actionable candidate and its exact run result", async () => {
    // A hard kill can leave only the submitted user row. MessageItem does not
    // render footers on user bubbles, so the durable candidate needs a
    // standalone review gate until an assistant-owned row exists.
    historyResponse.data.chat_history = [
      {
        sender: "user",
        content: "build the candidate",
        created_at: "2026-09-08T12:00:00Z",
      },
    ];
    historyResponse.data.proposed_workflow = proposedWorkflowPayload({
      title: "Recovered draft",
    });
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-interrupted",
      revision: 3,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: "wr-exact",
    };
    historyResponse.data.proposed_workflow_run = {
      workflow_run_id: "wr-exact",
      status: "completed",
      available: true,
      failure_reason: null,
      outputs: [{ output_parameter_id: "op-metric", value: { metric: "42" } }],
    };

    await renderChat();

    expect(await screen.findByText("Associated test: completed")).toBeTruthy();
    expect(screen.getByText(/op-metric:.*42/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
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
    expect(
      screen.getByRole("button", { name: /1 proposal pending/ }),
    ).toBeTruthy();

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
    const chip = screen.getByRole("button", { name: /1 proposal pending/ });
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

  it("shows auto-accept in the composer and Turn off mid-turn makes that turn's verified fix wait for review", async () => {
    historyResponse.data.auto_accept = true;
    await renderChat();
    const chip = await screen.findByRole("button", {
      name: /Auto-accepting/,
    });

    // The turn starts while auto-accept is still on.
    await submit("fix the selector");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));

    cancelPost.mockRejectedValueOnce({ response: { status: 500 } });
    await act(async () => {
      fireEvent.click(chip);
    });
    // A failed write leaves the server auto-accepting, so the chip must not claim otherwise.
    expect(screen.getByRole("button", { name: /Auto-accepting/ })).toBeTruthy();

    let finishTurnOff: (value: unknown) => void = () => {};
    cancelPost.mockImplementationOnce(
      () => new Promise((resolve) => (finishTurnOff = resolve)),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Auto-accepting/ }));
    });
    expect(cancelPost).toHaveBeenCalledWith(
      "/workflow/copilot/disable-auto-accept",
      { workflow_copilot_chat_id: "chat-1" },
    );
    historyResponse.data.auto_accept = false;

    // The server committed the write and finished the turn before the browser saw the POST resolve.
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Fixed and verified.", {
          proposal_disposition: "auto_applicable",
          workflow_applied: false,
          narrative_payload: proposalNarrativePayload({
            proposalDisposition: "auto_applicable",
          }),
        }),
      );
      streamCalls[0]!.resolve();
    });
    // The gate offers no actions while Turn off is pending; the draft must still be waiting when it finishes.
    await act(async () => {
      finishTurnOff({});
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /Auto-accepting/ }),
      ).toBeNull(),
    );
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
  });

  it.each([
    { rowKept: false, chipAfterAccept: "hidden" },
    // Apply writes auto-accept best-effort after creating the version, so the row can keep it on.
    { rowKept: true, chipAfterAccept: "shown" },
  ])(
    "after a plain Accept the chip follows the chat row (row kept auto_accept=$rowKept: $chipAfterAccept)",
    async ({ rowKept }) => {
      await renderChat();
      expect(
        screen.queryByRole("button", { name: /Auto-accepting/ }),
      ).toBeNull();

      await submit("build me a workflow");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
      await act(async () => {
        streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
        streamCalls[0]!.resolve();
      });
      historyResponse.data.auto_accept = true;
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Always accept" }));
      });
      expect(
        await screen.findByRole("button", { name: /Auto-accepting/ }),
      ).toBeTruthy();

      // Auto-accept never covers an untested draft, so this one still gets a gate.
      await submit("add a step");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
      await act(async () => {
        streamCalls[1]!.onMessage(
          proposalResponse("Untested draft.", {
            turn_id: "turn-2",
            narrative_payload: proposalNarrativePayload({
              turnId: "turn-2",
              turnIndex: 1,
            }),
          }),
        );
        streamCalls[1]!.resolve();
      });
      historyResponse.data.auto_accept = rowKept;
      await act(async () => {
        fireEvent.click(await screen.findByRole("button", { name: "Accept" }));
      });

      expect(cancelPost).toHaveBeenLastCalledWith(
        "/workflow/copilot/apply-proposed-workflow",
        expect.objectContaining({ auto_accept: false }),
      );
      await waitFor(() =>
        expect(
          Boolean(screen.queryByRole("button", { name: /Auto-accepting/ })),
        ).toBe(rowKept),
      );
    },
  );

  it("sends Turn off only after every in-flight Always accept settles, so auto-accept ends off", async () => {
    historyResponse.data.auto_accept = true;
    await renderChat();
    await submit("add a step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Untested draft."));
      streamCalls[0]!.resolve();
    });
    // The chat row takes each request's auto_accept when that request completes, as the routes do.
    const finishApplies: Array<(value: unknown) => void> = [];
    cancelPost.mockImplementation((path: string) => {
      if (path === "/workflow/copilot/apply-proposed-workflow") {
        return new Promise((resolve) => {
          finishApplies.push((value) => {
            historyResponse.data.auto_accept = true;
            resolve(value);
          });
        });
      }
      if (path === "/workflow/copilot/disable-auto-accept") {
        historyResponse.data.auto_accept = false;
      }
      return Promise.resolve({});
    });
    const requestedPaths = () => cancelPost.mock.calls.map(([path]) => path);

    // A double click starts two applies; the second can come back first.
    await act(async () => {
      fireEvent.click(
        await screen.findByRole("button", { name: "Always accept" }),
      );
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Always accept" }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Auto-accepting/ }));
    });
    expect(finishApplies).toHaveLength(2);
    await act(async () => {
      finishApplies[1]!({});
    });
    expect(requestedPaths()).not.toContain(
      "/workflow/copilot/disable-auto-accept",
    );

    await act(async () => {
      finishApplies[0]!({});
    });
    await waitFor(() =>
      expect(requestedPaths()).toContain(
        "/workflow/copilot/disable-auto-accept",
      ),
    );
    expect(
      requestedPaths().lastIndexOf("/workflow/copilot/apply-proposed-workflow"),
    ).toBeLessThan(
      requestedPaths().indexOf("/workflow/copilot/disable-auto-accept"),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /Auto-accepting/ }),
      ).toBeNull(),
    );
    expect(historyResponse.data.auto_accept).toBe(false);
  });

  it("offers no Accept while Turn off is pending, so no Accept can start after it", async () => {
    historyResponse.data.auto_accept = true;
    await renderChat();
    await submit("add a step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Untested draft."));
      streamCalls[0]!.resolve();
    });
    expect(
      await screen.findByRole("button", { name: "Always accept" }),
    ).toBeTruthy();
    let finishTurnOff: (value: unknown) => void = () => {};
    cancelPost.mockImplementation((path: string) =>
      path === "/workflow/copilot/disable-auto-accept"
        ? new Promise((resolve) => (finishTurnOff = resolve))
        : Promise.resolve({}),
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Auto-accepting/ }));
    });

    expect(screen.queryByRole("button", { name: "Always accept" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    // Neither writes auto_accept, so the user keeps them while Turn off runs.
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Review" })).toBeTruthy();

    await act(async () => {
      finishTurnOff({});
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /Auto-accepting/ }),
      ).toBeNull(),
    );
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
  });

  it("gives the gate its Accept back when an Accept never settles, instead of waiting forever", async () => {
    historyResponse.data.auto_accept = true;
    await renderChat();
    await submit("add a step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Untested draft."));
      streamCalls[0]!.resolve();
    });
    // An apply that never answers: without a ceiling the chat's gate would stay without Accept.
    cancelPost.mockImplementation((path: string) =>
      path === "/workflow/copilot/apply-proposed-workflow"
        ? new Promise(() => {})
        : Promise.resolve({}),
    );
    await act(async () => {
      fireEvent.click(
        await screen.findByRole("button", { name: "Always accept" }),
      );
    });
    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Auto-accepting/ }));
    });
    expect(screen.queryByRole("button", { name: "Always accept" })).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(ACCEPT_SETTLE_CEILING_MS + 1_000);
    });
    vi.useRealTimers();

    expect(
      await screen.findByRole("button", { name: "Always accept" }),
    ).toBeTruthy();
    // The disable never went out, so the chip still reports the row's state.
    expect(cancelPost.mock.calls.map(([path]) => path)).not.toContain(
      "/workflow/copilot/disable-auto-accept",
    );
    expect(screen.getByRole("button", { name: /Auto-accepting/ })).toBeTruthy();
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: "destructive" }),
    );
  });

  it("does not leak an Always accept into a New chat opened before it lands", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    let finishApply: (value: unknown) => void = () => {};
    cancelPost.mockImplementation((path: string) =>
      path === "/workflow/copilot/apply-proposed-workflow"
        ? new Promise((resolve) => (finishApply = resolve))
        : Promise.resolve({}),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Always accept" }));
    });

    // New chat clears the chat and its auto-accept while that apply is still in flight.
    historyResponse.data.auto_accept = false;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    });
    await act(async () => {
      finishApply({ data: { workflow_id: "wf_proposed" } });
    });

    // The apply landed for a chat the user already left: it must not arm the blank chat.
    expect(screen.queryByRole("button", { name: /Auto-accepting/ })).toBeNull();

    // And it stays off once the blank chat's own turn assigns it a chat id to read back.
    await submit("start over");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    await act(async () => {
      streamCalls[1]!.onMessage(
        proposalResponse("Renamed the blocks.", {
          turn_id: "turn-2",
        }),
      );
      streamCalls[1]!.resolve();
    });

    expect(await screen.findByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Auto-accepting/ })).toBeNull();
  });

  it("ignores a read that started before a plain Accept turned auto-accept off", async () => {
    historyResponse.data.auto_accept = true;
    await renderChat();
    await submit("add a step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Untested draft."));
      streamCalls[0]!.resolve();
    });
    expect(
      await screen.findByRole("button", { name: /Auto-accepting/ }),
    ).toBeTruthy();

    // A conflicted Accept leaves a row read in flight, taken while the row still said true.
    let finishStaleRead: (value: unknown) => void = () => {};
    historyGet.mockImplementationOnce(
      () => new Promise((resolve) => (finishStaleRead = resolve)),
    );
    cancelPost.mockRejectedValueOnce({ response: { status: 409 } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    // The retry succeeds: a plain Accept writes auto_accept=false on the row.
    historyResponse.data.auto_accept = false;
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Accept" }));
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /Auto-accepting/ }),
      ).toBeNull(),
    );

    await act(async () => {
      finishStaleRead({ data: { ...historyResponse.data, auto_accept: true } });
    });

    expect(screen.queryByRole("button", { name: /Auto-accepting/ })).toBeNull();
  });

  it("keeps Accept withheld until every Turn off for the chat settles, not just the first", async () => {
    historyResponse.data.auto_accept = true;
    await renderChat();
    await submit("add a step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Untested draft."));
      streamCalls[0]!.resolve();
    });
    const heldDisables: Array<(value: unknown) => void> = [];
    cancelPost.mockImplementation((path: string) =>
      path === "/workflow/copilot/disable-auto-accept"
        ? new Promise((resolve) => heldDisables.push(resolve))
        : Promise.resolve({}),
    );

    // Two clicks in one tick, before React re-renders: both start, as a double click does.
    const chip = await screen.findByRole("button", {
      name: /Auto-accepting/,
    });
    await act(async () => {
      fireEvent.click(chip);
      fireEvent.click(chip);
    });
    expect(heldDisables).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Always accept" })).toBeNull();

    // The first settles while the second is still in flight: the gate must stay withheld.
    await act(async () => {
      heldDisables[0]!({});
    });
    expect(screen.queryByRole("button", { name: "Always accept" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();

    await act(async () => {
      heldDisables[1]!({});
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy(),
    );
  });

  it("gives the gate its Accept back when Turn off fails", async () => {
    historyResponse.data.auto_accept = true;
    await renderChat();
    await submit("add a step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Untested draft."));
      streamCalls[0]!.resolve();
    });
    let failTurnOff: (reason: unknown) => void = () => {};
    cancelPost.mockImplementation((path: string) =>
      path === "/workflow/copilot/disable-auto-accept"
        ? new Promise((_resolve, reject) => (failTurnOff = reject))
        : Promise.resolve({}),
    );

    await act(async () => {
      fireEvent.click(
        await screen.findByRole("button", { name: /Auto-accepting/ }),
      );
    });
    expect(screen.queryByRole("button", { name: "Always accept" })).toBeNull();

    await act(async () => {
      failTurnOff({ response: { status: 500 } });
    });

    // The request failed, so auto-accept is still on and the gate must be usable again.
    expect(
      await screen.findByRole("button", { name: "Always accept" }),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: /Auto-accepting/ })).toBeTruthy();
  });

  it("keeps Turn off behind the fallback accept's row write", async () => {
    historyResponse.data.auto_accept = true;
    await renderChat();
    await submit("add a step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      // A v1 proposal carries no metadata, so a failed apply falls back to the client-side apply,
      // and that path writes the row itself instead of letting the apply route do it.
      streamCalls[0]!.onMessage(legacyProposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    let finishRowWrite: (value: unknown) => void = () => {};
    cancelPost.mockImplementation((path: string) => {
      if (path === "/workflow/copilot/clear-proposed-workflow") {
        return new Promise((resolve) => (finishRowWrite = resolve));
      }
      if (path === "/workflow/copilot/apply-proposed-workflow") {
        return Promise.reject({ response: { status: 500 } });
      }
      return Promise.resolve({});
    });
    await act(async () => {
      fireEvent.click(
        await screen.findByRole("button", { name: "Always accept" }),
      );
    });

    // That write ends with auto_accept=true, so a Turn off sent first is silently overwritten.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Auto-accepting/ }));
    });
    expect(cancelPost.mock.calls.map(([path]) => path)).not.toContain(
      "/workflow/copilot/disable-auto-accept",
    );

    await act(async () => {
      finishRowWrite({ data: {} });
    });
    await waitFor(() =>
      expect(cancelPost.mock.calls.map(([path]) => path)).toContain(
        "/workflow/copilot/disable-auto-accept",
      ),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /Auto-accepting/ }),
      ).toBeNull(),
    );
  });

  it("ignores a read-back that started before Turn off finished", async () => {
    historyResponse.data.auto_accept = true;
    await renderChat();
    await submit("add a step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Untested draft."));
      streamCalls[0]!.resolve();
    });
    let finishStaleRead: (value: unknown) => void = () => {};
    historyGet.mockImplementationOnce(
      () => new Promise((resolve) => (finishStaleRead = resolve)),
    );
    // A plain Accept reads the row back; that read captures auto_accept=true before Turn off lands.
    await act(async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Accept" }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Auto-accepting/ }));
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /Auto-accepting/ }),
      ).toBeNull(),
    );

    await act(async () => {
      finishStaleRead({ data: { ...historyResponse.data, auto_accept: true } });
    });

    expect(screen.queryByRole("button", { name: /Auto-accepting/ })).toBeNull();
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
    expect(
      screen.queryByRole("button", { name: /1 proposal pending/ }),
    ).toBeNull();
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
    expect(
      screen.queryByRole("button", { name: /1 proposal pending/ }),
    ).toBeNull();
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
