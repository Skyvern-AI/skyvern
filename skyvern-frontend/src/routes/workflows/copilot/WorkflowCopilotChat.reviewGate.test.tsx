import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
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
          disposition: "review_untested" | "accepting";
          workflow_run_id: string | null;
        } | null,
        proposed_claim_expires_in_seconds: null as number | null | undefined,
        proposed_workflow_run: null as {
          workflow_run_id: string;
          status: string | null;
          available: boolean;
          failure_reason: string | null;
          outputs: Array<{ output_parameter_id: string; value: unknown }>;
        } | null,
        auto_accept: false as boolean,
        question_interactions: [] as unknown[],
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

const { toast } = vi.hoisted(() => ({ toast: vi.fn() }));
vi.mock("@/components/ui/use-toast", () => ({ toast }));

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

// The real SaveButton renders beside the chat, so the save-block handoff runs through
// the real store; only its save hook is stubbed.
vi.mock("@/routes/workflows/editor/hooks/useSaveWorkflow", () => ({
  useSaveWorkflow: () => vi.fn(() => Promise.resolve()),
}));

// Unrelated to this file's tests; the real hook needs a QueryClientProvider
// this harness doesn't set up.
vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: undefined }),
}));

import { TooltipProvider } from "@/components/ui/tooltip";
import { SaveButton } from "@/routes/workflows/studio/StudioTopBar";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import { useCopilotHeaderStore } from "@/store/useCopilotHeaderStore";
import {
  useWorkflowHasChangesStore,
  type WorkflowSaveData,
} from "@/store/WorkflowHasChangesStore";
import { type WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";

import {
  fenceBaselineFor,
  hydratedGateFailure,
  extendedClaimHold,
  unattributedClaimDeadline,
} from "./acceptFence";
import {
  ACCEPT_SETTLE_CEILING_MS,
  WorkflowCopilotChat,
} from "./WorkflowCopilotChat";

type ChatProps = NonNullable<Parameters<typeof WorkflowCopilotChat>[0]>;

async function renderChat(props: Partial<ChatProps> = {}) {
  // docked renders via a portal; without a target it intentionally renders null.
  const portalTarget = props.docked ? document.body : undefined;
  const view = render(
    <WorkflowCopilotChat
      {...props}
      docked={props.docked ?? false}
      portalTarget={portalTarget}
    />,
  );
  await waitFor(() => expect(screen.getByRole("textbox")).toBeTruthy());
  return view;
}

function leaseDecrementingFrom(seconds: number | null | undefined): number {
  if (typeof seconds !== "number") {
    // null is a server saying no claim is held and undefined is one that cannot say. Neither
    // decrements, and the difference between them is load-bearing elsewhere.
    historyResponse.data.proposed_claim_expires_in_seconds = seconds;
    return 0;
  }
  const endsAt = Date.now() + seconds * 1000;
  Object.defineProperty(
    historyResponse.data,
    "proposed_claim_expires_in_seconds",
    {
      configurable: true,
      get: () => (endsAt - Date.now()) / 1000,
      set: (value: number | null | undefined) => {
        Object.defineProperty(
          historyResponse.data,
          "proposed_claim_expires_in_seconds",
          { configurable: true, enumerable: true, writable: true, value },
        );
      },
    },
  );
  return endsAt;
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
  useWorkflowHasChangesStore.setState({
    getSaveData: () => saveData as unknown as WorkflowSaveData,
    hasChanges: false,
    saveIsPending: false,
    saveBlockedReason: null,
  });
  toast.mockClear();
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLElement.prototype.scrollTo = vi.fn();
  streamCalls.length = 0;
  postStreaming.mockClear();
  // Reset, not clear: a test may queue a once-response its code path never consumes.
  cancelPost.mockReset();
  cancelPost.mockResolvedValue({});
  historyGet.mockReset();
  historyGet.mockImplementation(() => Promise.resolve(historyResponse));
  historyResponse.data = {
    workflow_copilot_chat_id: "chat-1",
    chat_history: [],
    proposed_workflow: null,
    proposed_workflow_metadata: null,
    proposed_claim_expires_in_seconds: null as number | null | undefined,
    proposed_workflow_run: null,
    auto_accept: false,
    question_interactions: [],
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
    // The reload swapped in a different proposal: the failure shows, but only a
    // fresh Accept of the proposal now on screen may save it.
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
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
    let finishReload: () => void = () => {};
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishReload = () => resolve(historyResponse);
        }),
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    // A rejected request may still have saved, so nothing is claimed until the chat row answers.
    expect(screen.queryByRole("alert")).toBeNull();
    await act(async () => {
      finishReload();
    });
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(cancelPost).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();

    cancelPost.mockResolvedValueOnce({ data: proposedWorkflowPayload() });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });

    expect(cancelPost).toHaveBeenLastCalledWith(
      "/workflow/copilot/apply-proposed-workflow",
      expect.objectContaining({ owner_turn_id: "turn-1", revision: 1 }),
    );
    expect(
      await screen.findByText("Accepted — saved to the workflow"),
    ).toBeTruthy();
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

  it("keeps a legacy proposal pending when Accept returns non-2xx, and only a fresh Accept can save what the reload shows", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    // A legacy proposal has no token, so the reload cannot prove it is the one that failed.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload({
      title: "Replacement draft",
    });
    cancelPost.mockRejectedValueOnce({
      response: {
        status: 400,
        data: { detail: "Proposed copilot YAML is invalid" },
      },
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    expect(screen.queryByText("Accepted — saved to the workflow")).toBeNull();
    expect(cancelPost).not.toHaveBeenCalledWith(
      "/workflow/copilot/clear-proposed-workflow",
      expect.anything(),
    );
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();

    cancelPost.mockResolvedValueOnce({ data: proposedWorkflowPayload() });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    expect(
      await screen.findByText("Accepted — saved to the workflow"),
    ).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("locks the gate while Accept is in flight so a second click cannot race it", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    let resolveApply: (value: unknown) => void = () => {};
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveApply = resolve;
        }),
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    for (const name of ["Accept", "Always accept", "Reject"]) {
      expect(screen.getByRole("button", { name }).matches(":disabled")).toBe(
        true,
      );
    }
    expect(screen.getByText("Accepting…")).toBeTruthy();

    await act(async () => {
      resolveApply({ data: proposedWorkflowPayload() });
    });
    expect(
      await screen.findByText("Accepted — saved to the workflow"),
    ).toBeTruthy();
  });

  it.each([false, true])(
    "locks History and New chat while an Accept is in flight (docked: %s)",
    async (docked) => {
      await renderChat({ docked });
      await submit("build me a workflow");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
      await act(async () => {
        streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
        streamCalls[0]!.resolve();
      });
      let resolveApply: (value: unknown) => void = () => {};
      cancelPost.mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveApply = resolve;
          }),
      );
      const navigationLocked = () => {
        const controls = useCopilotHeaderStore.getState().controls;
        return docked
          ? [controls!.disabled, controls!.navigationLockedReason !== null]
          : ["History", "New chat"].map((name) =>
              // The reason joins the accessible name once a control is locked.
              screen
                .getByRole("button", { name: new RegExp(`^${name}`) })
                .matches(":disabled"),
            );
      };
      expect(navigationLocked()).toEqual([false, false]);

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Accept" }));
      });

      // The server finishes an Accept regardless, so another chat must not take over the gate mid-apply.
      expect(navigationLocked()).toEqual([true, true]);
      // Nor may a new turn stage a proposal the late Accept would then clobber.
      await submit("change it again");
      expect(textarea().value).toBe("change it again");
      await act(async () => {
        resolveApply({ data: proposedWorkflowPayload() });
      });
      expect(navigationLocked()).toEqual([false, false]);
    },
  );

  // The live path that reaches this is the recovery poll, which no test in this file drives, so
  // the contract is asserted directly instead. See the PR's coverage-gaps note.
  describe("hydratedGateFailure", () => {
    // A claim the row can TIE TO A PROPOSAL. This fixture used to carry no metadata, which was
    // harmless only because the server derived the claim FROM the metadata - the two could not
    // disagree. It reports claim liveness independently now, so the combination below is a real
    // state with its own answer, and the assertions here would otherwise be testing it by
    // accident.
    const claimed = {
      proposed_claim_expires_in_seconds: 120,
      proposed_workflow_metadata: {
        owner_turn_id: "turn-1",
        revision: 1,
        canonical_fingerprint: "canonical-1",
        disposition: "accepting" as const,
        workflow_run_id: null,
      },
      auto_accept: false,
    };
    const unclaimed = { ...claimed, proposed_claim_expires_in_seconds: null };
    const claimedByAnother = {
      ...claimed,
      proposed_workflow_metadata: null,
    };
    const held = {
      kind: "recover",
      alwaysAccept: false,
      token: null,
      holdExpiresAt: 4_242,
      claimExpiresAtSeen: null,
    } as const;

    it("arms the fence when the row reports a claim", () => {
      const armed = hydratedGateFailure(claimed, null, 9_000);
      expect(armed?.kind).toBe("recover");
    });

    it("leaves an active hold alone when the row reports no claim", () => {
      // A read can overtake a POST that has not claimed yet, so "no claim" is not proof that
      // nothing is writing — releasing here is the hazard direction.
      expect(hydratedGateFailure(unclaimed, held, 9_000)).toBe(held);
    });

    it("stays clear when there is no hold and no claim", () => {
      expect(hydratedGateFailure(unclaimed, null, 9_000)).toBeNull();
    });

    it("keeps a confirmed save, which is the only copy of that workflow", () => {
      // `saved` means the apply returned 200 and its workflow is in hand while the canvas holds
      // the older draft. A recovery poll hydrating a row with no live claim must not drop it:
      // that discards the retry's only copy AND releases Save over a canvas known to be stale,
      // which is the overwrite this fence exists to prevent.
      const confirmed = {
        kind: "saved",
        alwaysAccept: false,
        token: null,
        ownerTurnId: "turn-1",
        savedWorkflow: { workflow_id: "wf_saved" } as unknown as never,
      } as const;
      expect(hydratedGateFailure(unclaimed, confirmed, 9_000)).toBe(confirmed);
    });

    it("does not downgrade a confirmed save to a claim that may still be writing", () => {
      // The route clears the claim AFTER the workflow write, best-effort, so a row can still
      // report a claim for an Accept that already landed. Arming over `saved` would drop the
      // only copy of the confirmed workflow and give the hold a deadline that can release over
      // a stale canvas - trading what we know for what we are guessing.
      const confirmed = {
        kind: "saved",
        alwaysAccept: false,
        token: null,
        ownerTurnId: "turn-1",
        savedWorkflow: { workflow_id: "wf_saved" } as unknown as never,
      } as const;
      expect(hydratedGateFailure(claimed, confirmed, 9_000)).toBe(confirmed);
    });

    it("still arms over every other hold when a row reports a claim", () => {
      // The suppression in `does not downgrade a confirmed save to a claim that may still be
      // writing` is specific to `saved`, which knows more than the row does.
      expect(hydratedGateFailure(claimed, held, 9_000)?.kind).toBe("recover");
      expect(hydratedGateFailure(claimed, null, 9_000)?.kind).toBe("recover");
    });

    it("lets a fresh row restate an outcome that claims nothing was saved", () => {
      const notSaved = {
        kind: "accept",
        alwaysAccept: false,
        token: null,
      } as const;
      expect(hydratedGateFailure(unclaimed, notSaved, 9_000)).toBeNull();
    });

    it("does not narrate a claim the row cannot tie to a proposal", () => {
      // `recover` says "this may have saved" and sends the user to Try again. Both are false
      // about another writer's claim - and with no proposal there is no card to press it on, so
      // the message would name a button that is not rendered. Save still holds, via
      // unattributedClaimDeadline, which says only what is true.
      expect(hydratedGateFailure(claimedByAnother, null, 9_000)).toBeNull();
    });
  });

  describe("fenceBaselineFor", () => {
    it("takes its reading only when no fence is open yet", () => {
      expect(fenceBaselineFor(undefined, 120_000)).toBe(120_000);
    });

    it("never moves an open fence's baseline", () => {
      expect(fenceBaselineFor(400, 300_000)).toBe(400);
    });

    it("keeps a blind fence blind", () => {
      // null is the ANSWER, not a missing one: the terminal pass treats an absent baseline as
      // grounds to hold, so letting an ordinary poll supply one from another writer's claim
      // would hand that rule a number to compare against and release.
      expect(fenceBaselineFor(null, 300_000)).toBeNull();
    });
  });

  describe("extendedClaimHold", () => {
    it("arms from a row when nothing is held yet", () => {
      expect(extendedClaimHold(null, 121_000)).toBe(121_000);
    });

    it("keeps the hold when a row reports no claim", () => {
      // A row covers ONE chat. The server reads the claim off that chat's own proposal blob, so
      // "no claim here" is not "no claim on this workflow" - and retiring on it releases Save
      // under another writer's live Accept.
      expect(extendedClaimHold(121_000, null)).toBe(121_000);
    });

    it("stays retired when nothing has ever armed it", () => {
      expect(extendedClaimHold(null, null)).toBeNull();
    });

    it("extends to a later claim", () => {
      expect(extendedClaimHold(121_000, 300_000)).toBe(300_000);
    });

    it("keeps the LATER deadline when a row reports a shorter one", () => {
      // MAX, NOT LAST. A chat-scoped read cannot say whose claim it is seeing, so a second
      // chat's shorter live claim is not evidence the first writer finished - taking the last
      // reading would release Save at 250 while a writer holding until 300 is still writing.
      expect(extendedClaimHold(300_000, 250_000)).toBe(300_000);
    });
  });

  describe("unattributedClaimDeadline", () => {
    const row = {
      proposed_claim_expires_in_seconds: 120,
      proposed_workflow_metadata: null,
    };

    it("holds until the reported lease runs out", () => {
      expect(unattributedClaimDeadline(row, 1_000)).toBe(121_000);
    });

    it("stays out of the way when the claim belongs to a visible proposal", () => {
      // That claim is the gate's business, not the workflow's - arming both would hold Save
      // twice for one write and leave this one outliving the gate that explains it.
      expect(
        unattributedClaimDeadline(
          {
            ...row,
            proposed_workflow_metadata: {
              owner_turn_id: "turn-1",
              revision: 1,
              canonical_fingerprint: "canonical-1",
              disposition: "accepting",
              workflow_run_id: null,
            },
          },
          1_000,
        ),
      ).toBeNull();
    });

    it("does not hold when no claim is reported", () => {
      expect(
        unattributedClaimDeadline(
          { ...row, proposed_claim_expires_in_seconds: null },
          1_000,
        ),
      ).toBeNull();
    });
  });

  it("never replaces the canvas during recovery, and keeps Save held once all it can say is that the canvas may be stale", async () => {
    const onWorkflowUpdate = vi.fn();
    await renderChat({ onWorkflowUpdate });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    // A real streamed draft, so the canvas is dirty exactly the way it is in production.
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    // The server saved and cleared the proposal; the canonical workflow is readable.
    const savedWorkflow = proposedWorkflowPayload({ workflow_id: "wf_saved" });
    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_claim_expires_in_seconds = 0.4;
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: savedWorkflow }
          : historyResponse,
      ),
    );
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    // The fence locks Save, sending and navigation but not canvas editing, and nothing tells
    // the user's work apart from the staged draft — so recovery must not overwrite it.
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(saveHeld()).toBe(true);
    expect(onWorkflowUpdate).not.toHaveBeenCalledWith(
      savedWorkflow,
      expect.anything(),
    );

    // Waiting on the server ends; the hold does not. The gate now says what the user is
    // looking at may be older than the server, and Save stays blocked, because a card cannot
    // tell the user to reload and leave live the one control that would overwrite what the
    // reload would bring back. The exit is the user's: Try again, or the reload it names.
    expect(
      await screen.findByText("Couldn't reload", undefined, { timeout: 4000 }),
    ).toBeTruthy();
    expect(saveHeld()).toBe(true);
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
    expect(onWorkflowUpdate).not.toHaveBeenCalledWith(
      savedWorkflow,
      expect.anything(),
    );
  });

  it("keeps the stale-canvas Save hold when a new send clears the gate card", async () => {
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    // The Accept's response is lost and the row comes back with no proposal, so recovery runs
    // out of attempts and the gate lands on "Couldn't reload" with Save held.
    historyResponse.data.proposed_workflow = null;
    leaseDecrementingFrom(0.4);
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(
      await screen.findByText(/Couldn't reload this proposal/, undefined, {
        timeout: 4000,
      }),
    ).toBeTruthy();
    expect(saveHeld()).toBe(true);

    // Sending is deliberately still allowed in this state, and it clears the gate card. The
    // hold is a fact about the CANVAS, not about the card, so it has to survive the card:
    // otherwise any message at all re-opens the destructive Save this state exists to prevent.
    await submit("actually, add a second step");
    expect(screen.queryByText(/Couldn't reload this proposal/)).toBeNull();
    expect(saveHeld()).toBe(true);
  });

  it("leaves the canvas current when the terminal recheck proves the Accept never saved", async () => {
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    // The row still carries the proposal, so the last-chance read proves canonical never moved
    // and this Accept wrote nothing.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    leaseDecrementingFrom(0.4);
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(saveHeld()).toBe(true);

    // staleCanvas is never SET in this sequence - the terminal read succeeds and reaches a
    // definite verdict, so recovery never gives up and never marks the canvas out of date. The
    // hold here is the FENCE's, and the fence ending is what releases Save.
    expect(
      await screen.findByText("Not saved", undefined, { timeout: 4000 }),
    ).toBeTruthy();
    await waitFor(() => expect(saveHeld()).toBe(false));
  });

  it("locks proposal actions after a failed reload, and Try again reconciles instead of re-sending Accept", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    cancelPost.mockRejectedValueOnce({ response: { status: 500 } });
    historyGet.mockRejectedValueOnce(new Error("network down"));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByRole("alert")).toBeTruthy();
    // The shown proposal may be stale, so nothing but the reload may act on it.
    for (const name of ["Accept", "Always accept", "Reject"]) {
      expect(screen.getByRole("button", { name }).matches(":disabled")).toBe(
        true,
      );
    }

    // A re-sent Accept would succeed here; Try again must only re-read the chat row.
    cancelPost.mockResolvedValueOnce({ data: proposedWorkflowPayload() });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Accept" }).matches(":disabled"),
      ).toBe(false),
    );
    expect(screen.queryByText("Accepted — saved to the workflow")).toBeNull();
  });

  it("shows a failed reload after a follow-up turn inline, with only the reload actionable", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    await submit("also grab the story scores");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    historyGet.mockRejectedValueOnce(new Error("network down"));

    // The turn ends without a new draft, so the kept proposal is re-read from the chat row.
    await act(async () => {
      streamCalls[1]!.onMessage(
        plainReplyResponse("I'll fold that into the draft above."),
      );
      streamCalls[1]!.resolve();
    });

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Accept" }).matches(":disabled"),
    ).toBe(true);

    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });

    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(
      screen.getByRole("button", { name: "Accept" }).matches(":disabled"),
    ).toBe(false);
  });

  it("holds a browser-queued prompt until a pending Accept settles, then sends it", async () => {
    const view = await renderChat({
      requiresLiveBrowser: true,
      isLiveBrowserReady: true,
    });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    view.rerender(
      <WorkflowCopilotChat
        docked={false}
        requiresLiveBrowser
        isLiveBrowserReady={false}
      />,
    );
    await submit("queued follow-up");
    let resolveApply: (value: unknown) => void = () => {};
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveApply = resolve;
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    view.rerender(
      <WorkflowCopilotChat
        docked={false}
        requiresLiveBrowser
        isLiveBrowserReady
        liveBrowserSessionId="pbs_harness"
      />,
    );
    const sent = (message: string) =>
      streamCalls.some((call) => call.body.message === message);
    // Let any send the effect would start reach the stream before asserting it did not.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(sent("queued follow-up")).toBe(false);

    await act(async () => {
      resolveApply({ data: proposedWorkflowPayload() });
    });
    await waitFor(() => expect(sent("queued follow-up")).toBe(true));
  });

  it("holds an initial message that arrives during a pending Accept, then sends it", async () => {
    const view = await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    let resolveApply: (value: unknown) => void = () => {};
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveApply = resolve;
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    // A slow Accept outlives the auto-send fallback window, which must not consume the message.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    view.rerender(
      <WorkflowCopilotChat docked={false} initialMessage="test the run" />,
    );
    await act(async () => {
      vi.advanceTimersByTime(6000);
    });
    vi.useRealTimers();
    const sent = (message: string) =>
      streamCalls.some((call) => call.body.message === message);
    // Let any send the effect would start reach the stream before asserting it did not.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(sent("test the run")).toBe(false);

    await act(async () => {
      resolveApply({ data: proposedWorkflowPayload() });
    });
    await waitFor(() => expect(sent("test the run")).toBe(true));
  });

  it.each([
    ["live", 240, true],
    ["expired", null, false],
    // An API instance that predates the field omits it; its row still names the claim.
    ["unreported by an older API", undefined, true],
  ] as const)(
    "after a lost Accept, a %s server claim decides whether the gate stays locked",
    async (_label, claimTtlSeconds, stillSaving) => {
      const metadata = {
        owner_turn_id: "turn-1",
        revision: 1,
        canonical_fingerprint: "canonical-1",
        disposition: "review_untested" as const,
        workflow_run_id: null,
      };
      await renderChat();
      await submit("build me a workflow");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
      await act(async () => {
        streamCalls[0]!.onMessage(
          proposalResponse("Draft ready.", {
            proposed_workflow_metadata: metadata,
          }),
        );
        streamCalls[0]!.resolve();
      });
      // The server claimed the proposal; within its lease it may still be writing the version,
      // past it the claim is abandoned and can be taken over or rejected.
      historyResponse.data.proposed_workflow = proposedWorkflowPayload();
      historyResponse.data.proposed_workflow_metadata = {
        ...metadata,
        disposition: "accepting" as unknown as "review_untested",
      } as typeof metadata;
      leaseDecrementingFrom(claimTtlSeconds);
      cancelPost.mockRejectedValueOnce(new Error("Network Error"));

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Accept" }));
      });

      expect(await screen.findByRole("alert")).toBeTruthy();
      // Try again is the only live control while the row is locked, so it must not be
      // swept into whatever disables the rest of the gate.
      expect(
        screen.getByRole("button", { name: "Try again" }).matches(":disabled"),
      ).toBe(false);
      for (const name of ["Accept", "Reject"]) {
        expect(screen.getByRole("button", { name }).matches(":disabled")).toBe(
          stillSaving,
        );
      }
    },
  );

  it("does not revive the previous chat's gate when a reload retry lands after New chat", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    await submit("also grab the story scores");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    historyGet.mockRejectedValueOnce(new Error("network down"));
    await act(async () => {
      streamCalls[1]!.onMessage(
        plainReplyResponse("I'll fold that into the draft above."),
      );
      streamCalls[1]!.resolve();
    });
    expect(await screen.findByRole("alert")).toBeTruthy();

    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    let finishReload: () => void = () => {};
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishReload = () => resolve(historyResponse);
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    });
    await act(async () => {
      finishReload();
    });

    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps a newer chat's reload failure when the previous chat's reload retry lands", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    await submit("also grab the story scores");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    historyGet.mockRejectedValueOnce(new Error("network down"));
    await act(async () => {
      streamCalls[1]!.onMessage(
        plainReplyResponse("I'll fold that into the draft above."),
      );
      streamCalls[1]!.resolve();
    });
    expect(await screen.findByRole("alert")).toBeTruthy();
    let finishOldReload: () => void = () => {};
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishOldReload = () => resolve(historyResponse);
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    });

    // The new chat stages its own proposal, and its Reject hits a conflict whose reload fails.
    await submit("build a different workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(3));
    await act(async () => {
      streamCalls[2]!.onMessage(
        proposalResponse("Second draft ready.", {
          workflow_copilot_chat_id: "chat-2",
          turn_id: "turn-2",
          narrative_payload: proposalNarrativePayload({ turnId: "turn-2" }),
        }),
      );
      streamCalls[2]!.resolve();
    });
    cancelPost.mockRejectedValueOnce({ response: { status: 409 } });
    historyGet.mockRejectedValueOnce(new Error("network down"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    });
    expect(await screen.findByRole("alert")).toBeTruthy();

    await act(async () => {
      finishOldReload();
    });

    expect(screen.getByRole("alert")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Accept" }).matches(":disabled"),
    ).toBe(true);
  });

  it.each(["the chat unmounts", "the claim lease expires"] as const)(
    "holds saves and chat navigation from Accept until %s",
    async (release) => {
      const view = await renderChat();
      render(
        <TooltipProvider>
          <SaveButton />
        </TooltipProvider>,
      );
      const saveHeld = () =>
        screen
          .getByRole("button", { name: /^Save workflow/ })
          .matches(":disabled");
      const locked = (name: string) =>
        screen
          .getByRole("button", { name: new RegExp(`^${name}`) })
          .matches(":disabled");
      await submit("build me a workflow");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
      await act(async () => {
        streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
        streamCalls[0]!.resolve();
      });
      historyGet.mockImplementation((path: string) =>
        path !== "/workflows/wpid_1"
          ? Promise.resolve(historyResponse)
          : Promise.reject(new Error("network down")),
      );
      if (release === "the claim lease expires") {
        // The server still holds the claim, with 1.5s of its lease left to run.
        historyResponse.data.proposed_workflow = proposedWorkflowPayload();
        historyResponse.data.proposed_workflow_metadata = {
          owner_turn_id: "turn-1",
          revision: 1,
          canonical_fingerprint: "canonical-1",
          disposition: "accepting" as unknown as "review_untested",
          workflow_run_id: null,
        } as typeof historyResponse.data.proposed_workflow_metadata;
        leaseDecrementingFrom(1.5);
      }
      let loseApplyResponse: () => void = () => {};
      cancelPost.mockImplementationOnce(
        () =>
          new Promise((_resolve, reject) => {
            loseApplyResponse = () => reject(new Error("Network Error"));
          }),
      );
      expect(saveHeld()).toBe(false);

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Accept" }));
      });
      // The server may be writing the accepted version from the moment Accept is sent.
      expect(saveHeld()).toBe(true);

      await act(async () => {
        loseApplyResponse();
      });
      expect(await screen.findByRole("alert")).toBeTruthy();
      expect(saveHeld()).toBe(true);
      expect(locked("History")).toBe(true);
      expect(locked("New chat")).toBe(true);
      expect(
        screen.getByRole("button", { name: "New chat" }).title,
      ).toBeTruthy();

      // A chat switch must not be able to release the hold.
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "New chat" }));
      });
      expect(saveHeld()).toBe(true);

      // Try again cannot end this either: recovery no longer adopts canonical, so retrying
      // re-reads and finds the same unresolved outcome.
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      });
      expect(saveHeld()).toBe(true);

      await act(async () => {
        if (release === "the chat unmounts") {
          view.unmount();
        }
      });
      await waitFor(() => expect(saveHeld()).toBe(false), { timeout: 4000 });
      if (release !== "the chat unmounts") {
        expect(locked("History")).toBe(false);
        expect(locked("New chat")).toBe(false);
      }
    },
    10_000,
  );

  it.each([
    ["a live server claim", "live-claim"],
    ["a chat row that will not load", "row-unreadable"],
    ["an API that cannot report the claim", "older-api"],
  ] as const)(
    "keeps saves and sends held while %s leaves the Accept unaccounted for",
    async (_label, mode) => {
      await renderChat();
      render(
        <TooltipProvider>
          <SaveButton />
        </TooltipProvider>,
      );
      const saveHeld = () =>
        screen
          .getByRole("button", { name: /^Save workflow/ })
          .matches(":disabled");
      await submit("build me a workflow");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
      await act(async () => {
        streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
        streamCalls[0]!.resolve();
      });
      if (mode === "row-unreadable") {
        historyGet.mockImplementation(() =>
          Promise.reject(new Error("network down")),
        );
      } else {
        historyResponse.data.proposed_workflow = proposedWorkflowPayload();
        historyResponse.data.proposed_claim_expires_in_seconds =
          mode === "live-claim" ? 120 : undefined;
        if (mode === "older-api") {
          // An instance that predates the field omits it; the row still names the claim,
          // and that claim can run for the backend's full five-minute lease.
          historyResponse.data.proposed_workflow_metadata = {
            owner_turn_id: "turn-1",
            revision: 1,
            canonical_fingerprint: "canonical-1",
            disposition: "accepting" as unknown as "review_untested",
            workflow_run_id: null,
          } as typeof historyResponse.data.proposed_workflow_metadata;
        }
      }
      cancelPost.mockRejectedValueOnce(new Error("Network Error"));

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Accept" }));
      });
      expect(await screen.findByRole("alert")).toBeTruthy();

      // A new turn would stage a proposal over the one the server may be writing, and
      // sending clears the gate failure, which would release the hold with it.
      await submit("change it again");
      expect(postStreaming).toHaveBeenCalledTimes(1);
      expect(saveHeld()).toBe(true);

      // A fence sized to a re-read rather than to the lease would have lapsed by now and let
      // the next Save duplicate the version the backend is still writing.
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 3_500));
      });
      expect(saveHeld()).toBe(true);
    },
    10_000,
  );

  it("keeps the fence when the editor cannot apply the workflow the server already saved", async () => {
    const onWorkflowUpdate = vi.fn(() => {
      throw new Error("could not parse the saved workflow");
    });
    await renderChat({ onWorkflowUpdate });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    // The server saved and cleared the proposal, but the response was lost, so recovery
    // reads canonical — and the editor cannot take it.
    historyResponse.data.proposed_workflow = null;
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: proposedWorkflowPayload() }
          : historyResponse,
      ),
    );
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    // The canvas still holds the pre-Accept graph, so releasing here would write it back
    // over the version the server kept.
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled"),
    ).toBe(true);
  });

  it("retries the workflow the Accept confirmed, instead of re-reading the chat row", async () => {
    let editorAccepts = false;
    const applied: WorkflowApiResponse[] = [];
    const onWorkflowUpdate = vi.fn((workflow: WorkflowApiResponse) => {
      if (!editorAccepts) {
        throw new Error("editor could not load it");
      }
      applied.push(workflow);
    });
    await renderChat({ onWorkflowUpdate });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    // The apply SUCCEEDS - 200, the saved workflow is in hand - but the editor throws.
    const savedWorkflow = proposedWorkflowPayload({
      workflow_id: "wf_saved",
    }) as unknown as WorkflowApiResponse;
    cancelPost.mockResolvedValueOnce({ data: savedWorkflow });
    const readsBefore = historyGet.mock.calls.length;

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    // Not an ambiguous outcome, so recovery must not run: no chat-row read at all.
    expect(await screen.findByText("Saved, not shown")).toBeTruthy();
    expect(historyGet.mock.calls.length).toBe(readsBefore);
    expect(saveHeld()).toBe(true);
    // Hydration refuses to arm a claim over this gate, which is only safe because no SECOND
    // Accept can be started from it to claim one - the action row is locked here.
    expect(
      screen.getByRole("button", { name: "Accept" }).matches(":disabled"),
    ).toBe(true);

    // Try again re-applies the workflow the server confirmed.
    editorAccepts = true;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });

    expect(applied).toContainEqual(savedWorkflow);
    await waitFor(() => expect(saveHeld()).toBe(false));
    expect(
      await screen.findByText("Accepted — saved to the workflow"),
    ).toBeTruthy();
  });

  it("keeps auto-accept on when a row read from before a saved Always accept lands last", async () => {
    // The apply's 200 means the server may have written auto_accept, so a row read issued before
    // it can no longer be trusted, even though the editor refused the workflow. The held row
    // reports an unattributed claim, which arms the Save hold from any row: that is how the test
    // knows the read landed.
    let editorAccepts = false;
    const onWorkflowUpdate = vi.fn((workflow: WorkflowApiResponse) => {
      if (!editorAccepts) {
        throw new Error("editor could not load it");
      }
      return workflow;
    });
    historyResponse.data.auto_accept = false;
    await renderChat({ onWorkflowUpdate });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    // A follow-up turn with no new draft re-reads the row for the bypassed proposal. Hold it open.
    let staleReadIssued = false;
    let releaseStaleRead: () => void = () => {};
    const staleRow = {
      data: {
        ...historyResponse.data,
        proposed_workflow: null,
        proposed_workflow_metadata: null,
        proposed_claim_expires_in_seconds: 100,
        auto_accept: false,
      },
    };
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          staleReadIssued = true;
          releaseStaleRead = () => resolve(staleRow);
        }),
    );
    await submit("also grab the story scores");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    await act(async () => {
      streamCalls[1]!.onMessage(
        plainReplyResponse("I'll fold that into the draft above."),
      );
      streamCalls[1]!.resolve();
    });
    await waitFor(() => expect(staleReadIssued).toBe(true));

    cancelPost.mockResolvedValueOnce({
      data: proposedWorkflowPayload({ workflow_id: "wf_saved" }),
    });
    historyResponse.data.auto_accept = true;
    historyResponse.data.proposed_workflow = null;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Always accept" }));
    });
    expect(await screen.findByText("Saved, not shown")).toBeTruthy();
    editorAccepts = true;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    expect(await screen.findByText("Auto-accepting")).toBeTruthy();
    expect(saveHeld()).toBe(false);

    await act(async () => {
      releaseStaleRead();
    });
    await waitFor(() => expect(saveHeld()).toBe(true));
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(screen.getByText("Auto-accepting")).toBeTruthy();
  });

  it("does not hold Save when a post-Accept read fails, since no card is shown", async () => {
    const applied: WorkflowApiResponse[] = [];
    const onWorkflowUpdate = vi.fn((workflow: WorkflowApiResponse) => {
      applied.push(workflow);
      return workflow;
    });
    historyResponse.data.auto_accept = false;
    await renderChat({ onWorkflowUpdate });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    // A SUCCESSFUL Always accept: 200, the editor takes the workflow. auto_accept was off, so
    // `alwaysAccept !== autoAcceptRef.current` and the post-Accept re-read is issued. It fails.
    cancelPost.mockResolvedValueOnce({
      data: proposedWorkflowPayload({ workflow_id: "wf_ok" }),
    });
    historyGet.mockRejectedValueOnce(new Error("network down"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Always accept" }));
    });
    await waitFor(() => expect(applied.length).toBeGreaterThan(0));

    // The Accept SUCCEEDED and the editor loaded the result as its clean baseline.
    expect(screen.queryByText("Saved, not shown")).toBeNull();
    // No card renders here (the proposal is cleared and the gate is not `saved`), so there is no
    // Try again and no named exit. A hold would be unreleasable until a reload - and a failed row
    // read is an availability fact, not evidence that canonical moved under the editor.
    expect(screen.queryByText("Couldn't reload")).toBeNull();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    expect(saveHeld()).toBe(false);
  });

  it("holds Save whenever the reload card is on screen, whichever read armed it", async () => {
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    // A follow-up turn with no new draft re-reads the row for the bypassed proposal (:5132).
    // That read FAILS. The proposal is still staged, so the reload card has a subject.
    historyGet.mockRejectedValueOnce(new Error("network down"));
    await submit("also grab the story scores");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    await act(async () => {
      streamCalls[1]!.onMessage(
        plainReplyResponse("I'll fold that into the draft above."),
      );
      streamCalls[1]!.resolve();
    });

    // This caller has no evidence canonical moved, but the CARD is what carries the contract:
    // it tells the user the canvas may be stale, so Save must not sit live beside it.
    expect(screen.queryByText("Couldn't reload")).toBeTruthy();
    expect(saveHeld()).toBe(true);

    // And the hold ENDS WITH THE CARD, which is the property this shape was chosen for. Try again
    // re-reads successfully, the gate clears, the card goes - and Save must come back. A hold
    // derived from the failed READ rather than from the card outlives the card here, which is how
    // the earlier shape stranded the user with a message that was no longer on screen.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    await waitFor(() =>
      expect(screen.queryByText("Couldn't reload")).toBeNull(),
    );
    expect(saveHeld()).toBe(false);
  });

  it("takes auto-accept from the server after a saved retry, not from the attempt", async () => {
    let editorAccepts = false;
    const applied: WorkflowApiResponse[] = [];
    const onWorkflowUpdate = vi.fn((workflow: WorkflowApiResponse) => {
      if (!editorAccepts) {
        throw new Error("editor could not load it");
      }
      applied.push(workflow);
    });
    // Mount with auto-accept OFF, the way the session actually starts: the server only
    // persists it during the apply below. Seeding it true before mount would let hydration
    // set the flag and the test would pass without reading anything.
    historyResponse.data.auto_accept = false;
    await renderChat({ onWorkflowUpdate });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    const savedWorkflow = proposedWorkflowPayload({
      workflow_id: "wf_saved",
    }) as unknown as WorkflowApiResponse;
    cancelPost.mockResolvedValueOnce({ data: savedWorkflow });
    // The apply persists auto_accept server-side. Its 200 does not carry that fact - the
    // route writes it after the workflow write, inside a best-effort swallow - so the only
    // way the client can know is to read it back.
    historyResponse.data.auto_accept = true;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Always accept" }));
    });
    expect(await screen.findByText("Saved, not shown")).toBeTruthy();

    editorAccepts = true;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    expect(applied).toContainEqual(savedWorkflow);

    // Reading one field must not re-hydrate the row: the gate this retry just cleared may
    // not come back.
    await waitFor(() => expect(historyGet).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText("Confirming\u2026")).toBeNull();
    expect(screen.queryByText("Couldn't reload")).toBeNull();
    expect(screen.queryByText("Saved, not shown")).toBeNull();

    // And the client now holds the server's value, which is what the user experiences.
    //
    // CHANGED BY THE #17099 MERGE, AND THE MECHANISM IS GONE WHILE THE CONCERN SURVIVES. This
    // used to assert that the next auto-applicable proposal AUTO-APPLIED, because the client's
    // flag drove that decision. #17099 moved the decision to the server -
    // shouldAutoApplyWorkflowResponse no longer takes autoAccept and now requires
    // `workflow_applied === true` - so that assertion can no longer distinguish a client that
    // re-read auto_accept from one that did not. Adding `workflow_applied: true` to the response
    // would turn this green in a line and make it VACUOUS FOR ITS OWN PURPOSE: it would pass
    // whether or not the read ever happened.
    //
    // The concern - the client must not carry a STALE auto_accept forward - is still live, so it
    // is asserted where the value still has an observable effect. The chip renders on exactly
    // `autoAccept && workflowCopilotChatId`, so its presence IS the client's flag, and it is the
    // control through which the user turns auto-accept off. Asserting the request body would be
    // stronger, but nothing sends this state: the apply POST sends the button (`alwaysAccept`)
    // and clearProposedWorkflow's only caller passes a literal false.
    expect(await screen.findByText("Auto-accepting")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Turn off/ })).toBeTruthy();
  });

  it("does not carry a saved retry's auto-accept read into a new chat", async () => {
    let editorAccepts = false;
    const onWorkflowUpdate = vi.fn((workflow: WorkflowApiResponse) => {
      if (!editorAccepts) {
        throw new Error("editor could not load it");
      }
      return workflow;
    });
    historyResponse.data.auto_accept = false;
    await renderChat({ onWorkflowUpdate });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    const savedWorkflow = proposedWorkflowPayload({
      workflow_id: "wf_saved",
    }) as unknown as WorkflowApiResponse;
    cancelPost.mockResolvedValueOnce({ data: savedWorkflow });
    historyResponse.data.auto_accept = true;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Always accept" }));
    });
    expect(await screen.findByText("Saved, not shown")).toBeTruthy();

    // Hold the post-retry read open so the chat switch happens while it is in flight.
    let releaseRow: () => void = () => {};
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseRow = () => resolve(historyResponse);
        }),
    );
    editorAccepts = true;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    // Clearing the gate unlocks navigation, so the user can start a new chat before the old
    // chat's row comes back - and New chat resets auto-accept to off.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    });
    await act(async () => {
      releaseRow();
    });

    // The old chat's setting must not land in the new one: its next auto-applicable proposal
    // waits for review instead of being applied locally past the gate.
    const nextWorkflow = proposedWorkflowPayload({
      workflow_id: "wf_next",
    }) as unknown as WorkflowApiResponse;
    await submit("start something else");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    await act(async () => {
      streamCalls[1]!.onMessage(
        proposalResponse("New draft.", {
          turn_id: "turn-2",
          proposal_disposition: "auto_applicable",
          updated_workflow: nextWorkflow,
        }),
      );
      streamCalls[1]!.resolve();
    });
    expect(onWorkflowUpdate).not.toHaveBeenCalledWith(
      nextWorkflow,
      expect.objectContaining({ applied: true }),
    );
  });

  it("does not let a late Reject revert the turn that replaced it", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    // Reject is not in the fence, so the composer stays live while its POST is in flight.
    let releaseReject: () => void = () => {};
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseReject = () => resolve({ data: {} });
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    });

    // A new turn lands before the reject comes back, and keeps its own snapshot.
    await submit("actually, do it differently");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    await act(async () => {
      streamCalls[1]!.onMessage(
        proposalResponse("Second draft.", { turn_id: "turn-2" }),
      );
      streamCalls[1]!.resolve();
    });
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();

    await act(async () => {
      releaseReject();
    });

    // The reject belonged to the turn the user rejected, not to the one that replaced it:
    // landing it here would revert the new proposal off the canvas and mark it rejected.
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.queryByText("Rejected")).toBeNull();
  });

  it("does not let a late Reject clear the gate of a chat the user navigated to", async () => {
    await renderChat({ docked: true });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    let releaseReject: () => void = () => {};
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseReject = () => resolve({ data: {} });
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    });

    // The user picks another chat from History. That chat already has a proposal pending, so
    // it hydrates its own review gate - nothing here bumps the send epoch.
    historyResponse.data.workflow_copilot_chat_id = "chat-2";
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    await act(async () => {
      await useCopilotHeaderStore.getState().controls?.onSelectChat?.({
        workflow_copilot_chat_id: "chat-2",
      } as Parameters<
        NonNullable<
          NonNullable<
            ReturnType<typeof useCopilotHeaderStore.getState>["controls"]
          >["onSelectChat"]
        >
      >[0]);
    });
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();

    await act(async () => {
      releaseReject();
    });

    // The reject belonged to the chat the user left. Landing it here clears the selected
    // chat's proposal while its candidate is still pending on the server, leaving no gate.
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
  });

  it("still completes a Reject when the history navigation it raced fails", async () => {
    await renderChat({ docked: true });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    let releaseReject: () => void = () => {};
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseReject = () => resolve({ data: {} });
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    });

    // The user tries another chat and the load FAILS, so they stay where they are.
    historyGet.mockImplementationOnce(() =>
      Promise.reject(new Error("network down")),
    );
    await act(async () => {
      await useCopilotHeaderStore.getState().controls?.onSelectChat?.({
        workflow_copilot_chat_id: "chat-2",
      } as Parameters<
        NonNullable<
          NonNullable<
            ReturnType<typeof useCopilotHeaderStore.getState>["controls"]
          >["onSelectChat"]
        >
      >[0]);
    });

    await act(async () => {
      releaseReject();
    });

    // Nothing moved: the user is in the chat they rejected from, the server cleared the
    // candidate, so the draft and its gate must go. Bailing here would leave the rejected
    // draft on the canvas and saveable.
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Accept" })).toBeNull(),
    );
  });

  it("discards a history load the user abandoned", async () => {
    const onWorkflowUpdate = vi.fn();
    await renderChat({ docked: true, onWorkflowUpdate });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    // A history load the user will walk away from, carrying that chat's own settings.
    let releaseHistory: () => void = () => {};
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseHistory = () =>
            resolve({
              data: {
                ...historyResponse.data,
                workflow_copilot_chat_id: "chat-2",
                auto_accept: true,
                proposed_workflow: proposedWorkflowPayload(),
              },
            });
        }),
    );
    await act(async () => {
      void useCopilotHeaderStore.getState().controls?.onSelectChat?.({
        workflow_copilot_chat_id: "chat-2",
      } as Parameters<
        NonNullable<
          NonNullable<
            ReturnType<typeof useCopilotHeaderStore.getState>["controls"]
          >["onSelectChat"]
        >
      >[0]);
    });

    // The user gives up on it and starts a new chat, which clears the board.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    });
    // Only now does the abandoned load answer.
    await act(async () => {
      releaseHistory();
    });

    // It must not write the chat the user left back over the one they are in: no gate from a
    // proposal belonging to chat-2.
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();

    // And it must not restore that chat's Always accept either, which would auto-apply the
    // next proposal here without review.
    const nextWorkflow = proposedWorkflowPayload({
      workflow_id: "wf_next",
    }) as unknown as WorkflowApiResponse;
    await submit("something else entirely");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    await act(async () => {
      streamCalls[1]!.onMessage(
        proposalResponse("Fresh draft.", {
          turn_id: "turn-2",
          proposal_disposition: "auto_applicable",
          updated_workflow: nextWorkflow,
        }),
      );
      streamCalls[1]!.resolve();
    });
    expect(onWorkflowUpdate).not.toHaveBeenCalledWith(
      nextWorkflow,
      expect.objectContaining({ applied: true }),
    );
  });

  it("lifts the stale-canvas hold when the server itself commits a later turn", async () => {
    await renderChat({ onWorkflowUpdate: vi.fn() });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    historyResponse.data.proposed_workflow = null;
    leaseDecrementingFrom(0.4);
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(
      await screen.findByText(/Couldn't reload this proposal/, undefined, {
        timeout: 4000,
      }),
    ).toBeTruthy();
    expect(saveHeld()).toBe(true);

    // A later turn the SERVER commits itself. The canvas is canonical again, so the hold has
    // nothing left to protect - keeping Save disabled would tell the user their canvas is out
    // of date immediately after the server refreshed it.
    await submit("add a second step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    await act(async () => {
      streamCalls[1]!.onMessage(
        proposalResponse("Committed.", {
          turn_id: "turn-2",
          proposal_disposition: "auto_applicable",
          workflow_applied: true,
        }),
      );
      streamCalls[1]!.resolve();
    });
    await waitFor(() => expect(saveHeld()).toBe(false));
  });

  it("keeps the recovery gate's exit on screen after hydration clears the proposal, with its actions still locked", async () => {
    await renderChat({ docked: true });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    // The Accept's outcome is unknown and the row reports a live claim, so the gate parks on
    // `recover` - whose Save reason tells the user to use Try again on the review gate.
    historyResponse.data.proposed_claim_expires_in_seconds = 120;
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByText("Confirming…")).toBeTruthy();
    expect(saveHeld()).toBe(true);

    // A hydration lands with no proposal and no live claim: the gate is PRESERVED while its
    // subject goes away. Without this fix the card stops rendering and the Save reason names a
    // Try again that is not on screen - a hold with no exit but a page reload.
    historyGet.mockImplementationOnce(() =>
      Promise.resolve({
        data: {
          ...historyResponse.data,
          workflow_copilot_chat_id: "chat-2",
          proposed_workflow: null,
          proposed_workflow_metadata: null,
          proposed_claim_expires_in_seconds: null,
        },
      }),
    );
    await act(async () => {
      await useCopilotHeaderStore.getState().controls?.onSelectChat?.({
        workflow_copilot_chat_id: "chat-2",
      } as Parameters<
        NonNullable<
          NonNullable<
            ReturnType<typeof useCopilotHeaderStore.getState>["controls"]
          >["onSelectChat"]
        >
      >[0]);
    });

    // Half one: the exit the Save reason names is on screen.
    const retry = await screen.findByRole("button", { name: "Try again" });
    expect(retry.matches(":disabled")).toBe(false);
    // Half two, and it is the half that stops this fix becoming a defect: rendering the card must
    // NOT re-enable the proposal actions. `gateActionable` is true here; what holds the line is
    // ReviewGateCard's own disabled fieldset.
    expect(
      screen.getByRole("button", { name: "Accept" }).matches(":disabled"),
    ).toBe(true);
    expect(
      screen.getByRole("button", { name: "Reject" }).matches(":disabled"),
    ).toBe(true);
  });

  it("keeps the confirmed-save retry reachable after hydration clears the proposal", async () => {
    let editorAccepts = true;
    const applied: WorkflowApiResponse[] = [];
    const onWorkflowUpdate = vi.fn((workflow: WorkflowApiResponse) => {
      if (!editorAccepts) {
        throw new Error("editor could not load it");
      }
      applied.push(workflow);
    });
    await renderChat({ docked: true, onWorkflowUpdate });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    // The apply returns 200 and the editor throws, so the gate parks on `saved`.
    const savedWorkflow = proposedWorkflowPayload({
      workflow_id: "wf_saved",
    }) as unknown as WorkflowApiResponse;
    editorAccepts = false;
    cancelPost.mockResolvedValueOnce({ data: savedWorkflow });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByText("Saved, not shown")).toBeTruthy();
    expect(saveHeld()).toBe(true);

    // A hydration lands carrying no proposal - which is the truth after a save: the server
    // cleared it. The recovery poll reaches this path without touching the fence.
    historyGet.mockImplementationOnce(() =>
      Promise.resolve({
        data: {
          ...historyResponse.data,
          workflow_copilot_chat_id: "chat-2",
          proposed_workflow: null,
        },
      }),
    );
    await act(async () => {
      await useCopilotHeaderStore.getState().controls?.onSelectChat?.({
        workflow_copilot_chat_id: "chat-2",
      } as Parameters<
        NonNullable<
          NonNullable<
            ReturnType<typeof useCopilotHeaderStore.getState>["controls"]
          >["onSelectChat"]
        >
      >[0]);
    });

    // The hold has no deadline, so its exit has to survive the proposal going away. A card
    // with no Try again would be a hold the user cannot end except by reloading the page.
    const retry = await screen.findByRole("button", { name: "Try again" });
    expect(retry.matches(":disabled")).toBe(false);

    editorAccepts = true;
    await act(async () => {
      fireEvent.click(retry);
    });
    expect(applied).toContainEqual(savedWorkflow);
    await waitFor(() => expect(saveHeld()).toBe(false));
  });

  it("does not let an abandoned initial history read restore Always accept", async () => {
    const onWorkflowUpdate = vi.fn();
    // The INITIAL history read pends, carrying the old chat's Always accept.
    let releaseMount: () => void = () => {};
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseMount = () =>
            resolve({
              data: {
                ...historyResponse.data,
                workflow_copilot_chat_id: "chat-old",
                auto_accept: true,
              },
            });
        }),
    );
    await renderChat({ docked: true, onWorkflowUpdate });
    // New chat is reachable here: it neither unmounts the component nor is disabled while
    // history loads, because `newChatDisabled` keys on the Accept hold only.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    });
    await act(async () => {
      releaseMount();
    });

    const nextWorkflow = proposedWorkflowPayload({
      workflow_id: "wf_next",
    }) as unknown as WorkflowApiResponse;
    await submit("something new");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Draft.", {
          turn_id: "turn-9",
          proposal_disposition: "auto_applicable",
          updated_workflow: nextWorkflow,
        }),
      );
      streamCalls[0]!.resolve();
    });

    // The abandoned read must not put the old chat's Always accept back: if it does, this
    // proposal is applied locally and the user never sees the review gate.
    //
    // READ THIS BEFORE DELETING THIS TEST. Dropping the New chat click above makes it fail,
    // and that is NOT the test failing to discriminate. Without New chat the hydration
    // legitimately sets Always accept and applying IS correct. The discriminator is the pair:
    // WITH New chat the right answer is "not applied", and before the epoch guard we observed
    // "applied". The single useful control is the stale row carrying auto_accept false, which
    // makes this assertion pass for the boring reason.
    expect(onWorkflowUpdate).not.toHaveBeenCalledWith(
      nextWorkflow,
      expect.objectContaining({ applied: true }),
    );
  });

  // The gate owner's row decides WHICH site renders the card, so both shapes are driven here
  // against each other. A narrative row renders it inline at the narrative site; a plain row
  // has no narrative branch and must fall to the message footer. Both suppress the standalone
  // fallback, so each arm also asserts EXACTLY ONE card - a second card is as wrong as none,
  // and a single-element query would pass either way.
  it.each([
    ["a narrative row", true],
    ["a plain row with no narrative", false],
  ])(
    "keeps the confirmed-save exit reachable when the owner card is inline: %s",
    async (_label, withNarrative) => {
      let editorAccepts = true;
      const applied: WorkflowApiResponse[] = [];
      const onWorkflowUpdate = vi.fn((workflow: WorkflowApiResponse) => {
        if (!editorAccepts) {
          throw new Error("editor could not load it");
        }
        applied.push(workflow);
      });
      await renderChat({ docked: true, onWorkflowUpdate });
      render(
        <TooltipProvider>
          <SaveButton />
        </TooltipProvider>,
      );
      const saveHeld = () =>
        screen
          .getByRole("button", { name: /^Save workflow/ })
          .matches(":disabled");
      await submit("build me a workflow");
      await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
      await act(async () => {
        streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
        streamCalls[0]!.resolve();
      });

      const savedWorkflow = proposedWorkflowPayload({
        workflow_id: "wf_saved",
      }) as unknown as WorkflowApiResponse;
      editorAccepts = false;
      cancelPost.mockResolvedValueOnce({ data: savedWorkflow });
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Accept" }));
      });
      expect(await screen.findByText("Saved, not shown")).toBeTruthy();
      expect(saveHeld()).toBe(true);

      // A hydration for THIS chat lands: the proposal is correctly gone - the server cleared it
      // on the save - but the turn that owned it is still in the history, so its card still
      // renders inline, which suppresses the standalone gate.
      historyGet.mockImplementationOnce(() =>
        Promise.resolve({
          data: {
            ...historyResponse.data,
            proposed_workflow: null,
            chat_history: [
              {
                sender: "ai",
                content: "Draft ready.",
                created_at: new Date().toISOString(),
                ...(withNarrative
                  ? { narrative_payload: proposalNarrativePayload() }
                  : {}),
              },
            ],
          },
        }),
      );
      await act(async () => {
        await useCopilotHeaderStore.getState().controls?.onSelectChat?.({
          workflow_copilot_chat_id: "chat-rehydrate",
        } as Parameters<
          NonNullable<
            NonNullable<
              ReturnType<typeof useCopilotHeaderStore.getState>["controls"]
            >["onSelectChat"]
          >
        >[0]);
      });

      // The hold has no deadline, so the exit has to survive the proposal going away - including
      // when the inline owner card is the one rendering the gate.
      expect(await screen.findByText("Saved, not shown")).toBeTruthy();
      expect(screen.getAllByText("Saved, not shown")).toHaveLength(1);
      const retry = await screen.findByRole("button", { name: "Try again" });
      expect(retry.matches(":disabled")).toBe(false);

      editorAccepts = true;
      await act(async () => {
        fireEvent.click(retry);
      });
      expect(applied).toContainEqual(savedWorkflow);
      await waitFor(() => expect(saveHeld()).toBe(false));
      // The RECEIPT, which is the whole reason the `saved` gate carries its own owner turn:
      // hydration cleared pendingProposalTurnId, so without the carried id the retry marks
      // nothing and a save that definitely landed reports nothing. Asserting the applied
      // workflow alone leaves that regression invisible - both arms stay green with the carried
      // id replaced by null.
      //
      // Only the narrative row can show it: `settled` is computed at the narrative render site
      // and hardcoded to null at the message-footer and fallback sites, so a plain row's card
      // simply disappears when the gate clears. That asymmetry is a separate gap, recorded here
      // rather than asserted away - the plain arm checks the half it CAN observe.
      if (withNarrative) {
        expect(
          await screen.findByText("Accepted — saved to the workflow"),
        ).toBeTruthy();
      } else {
        await waitFor(() =>
          expect(screen.queryByText("Saved, not shown")).toBeNull(),
        );
      }
    },
  );

  it("keeps the stale-canvas hold, and its exit, when only an Accept outcome is proven", async () => {
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    historyResponse.data.proposed_workflow = null;
    leaseDecrementingFrom(0.4);
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(
      await screen.findByText(/Couldn't reload this proposal/, undefined, {
        timeout: 4000,
      }),
    ).toBeTruthy();
    expect(saveHeld()).toBe(true);

    // Try again now reads a row whose proposal is still there AND carries metadata. The server
    // gates a tokenized proposal on the canonical fingerprint, so its survival proves canonical
    // never moved - nothing was saved, and the canvas nothing overtook is not stale.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: null,
    };
    historyResponse.data.proposed_claim_expires_in_seconds = null;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });

    // The hold STANDS: no persisted workflow reached the editor anywhere in this sequence.
    await waitFor(() => expect(saveHeld()).toBe(true));

    // And its exit is ON SCREEN in this state rather than merely written somewhere: the disabled
    // Save control carries the reload instruction in its ACCESSIBLE NAME, which is what a user
    // who presses Save actually meets. This is the difference between "the exit exists" and
    // "the exit renders in the state this hold makes more common".
    expect(
      screen.getByRole("button", { name: /reload the page first/i }),
    ).toBeTruthy();
  });

  it("keeps the fence when the apply succeeds but the editor cannot take the saved workflow", async () => {
    const onWorkflowUpdate = vi.fn(() => {
      throw new Error("could not parse the saved workflow");
    });
    await renderChat({ onWorkflowUpdate });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    // The apply returns 200 — the version exists — and the canonical re-read that would
    // catch the canvas up is unavailable.
    cancelPost.mockResolvedValueOnce({ data: proposedWorkflowPayload() });
    historyGet.mockImplementation(() =>
      Promise.reject(new Error("network down")),
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    // The studio's own handler shows "Update failed" and re-throws so this boolean is honest;
    // the wrapper must not add a second toast for the same event.
    expect(toast).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: "Update failed" }),
    );
    // The server holds the accepted version and the canvas still holds the pre-Accept graph,
    // so releasing here would write the stale one back over it.
    await waitFor(() =>
      expect(
        screen
          .getByRole("button", { name: /^Save workflow/ })
          .matches(":disabled"),
      ).toBe(true),
    );
  });

  it("does not let a manual retry extend the fence past the lease it was opened for", async () => {
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_claim_expires_in_seconds = 0.5;
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(saveHeld()).toBe(true);

    // Clicking Try again against a chat row that will not load must not open a fresh
    // five-minute fence: the claim it is waiting on expires when it always did, and the gate
    // hands over to the one the user ends. A retry that reopened the lease would still be
    // saying "Confirming…" here.
    historyGet.mockImplementation(() =>
      Promise.reject(new Error("network down")),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    expect(
      await screen.findByText(/Couldn't reload this proposal/, undefined, {
        timeout: 4000,
      }),
    ).toBeTruthy();
    expect(screen.queryByText("Confirming…")).toBeNull();
    // Waiting on the server ended on schedule; the stale canvas it left behind still holds.
    expect(saveHeld()).toBe(true);
  });

  it.each([
    ["reports the remainder", 120],
    ["is too old to report it", undefined],
  ] as const)(
    "restores the Accept fence when a reload lands while a server that %s still holds its claim",
    async (_label, claimTtlSeconds) => {
      // The tab reloads after Accept reached the backend and before it finished.
      historyResponse.data.proposed_workflow = proposedWorkflowPayload();
      historyResponse.data.proposed_workflow_metadata = {
        owner_turn_id: "turn-1",
        revision: 1,
        canonical_fingerprint: "canonical-1",
        disposition: "accepting" as unknown as "review_untested",
        workflow_run_id: null,
      } as typeof historyResponse.data.proposed_workflow_metadata;
      leaseDecrementingFrom(claimTtlSeconds);

      await renderChat();
      render(
        <TooltipProvider>
          <SaveButton />
        </TooltipProvider>,
      );

      // Hydrating this as an ordinary pending proposal would leave Save and the gate live
      // while the server may still be creating the version.
      await waitFor(() =>
        expect(
          screen
            .getByRole("button", { name: /^Save workflow/ })
            .matches(":disabled"),
        ).toBe(true),
      );
      for (const name of ["Accept", "Reject"]) {
        expect(screen.getByRole("button", { name }).matches(":disabled")).toBe(
          true,
        );
      }
    },
  );

  it("does not call a failed Accept applied when the surviving proposal still matches canonical", async () => {
    const onWorkflowUpdate = vi.fn();
    useWorkflowHasChangesStore.setState({
      getSaveData: () =>
        ({
          ...saveData,
          workflow: { ...saveData.workflow, version: 3 },
        }) as unknown as WorkflowSaveData,
    });
    await renderChat({ onWorkflowUpdate });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    // The row still carries the proposal AND its metadata, which the history route only
    // returns while canonical matches the fingerprint the proposal was built against — so
    // canonical being ahead of the editor says the editor is stale, not that this Accept won.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested" as const,
      workflow_run_id: null,
    };
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1"
          ? { data: { ...proposedWorkflowPayload(), version: 4 } }
          : historyResponse,
      ),
    );
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    // Whichever failure it settles on, it may not be the one that claims success.
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByText("Accepted — saved to the workflow")).toBeNull();
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
  });

  it("never reports a failed Accept as applied, even when a newer canonical version appeared", async () => {
    const onWorkflowUpdate = vi.fn();
    useWorkflowHasChangesStore.setState({
      getSaveData: () =>
        ({
          ...saveData,
          workflow: { ...saveData.workflow, version: 3 },
        }) as unknown as WorkflowSaveData,
    });
    await renderChat({ onWorkflowUpdate });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    // The version was created but its proposal cleanup failed, and the response was lost.
    const created = { ...proposedWorkflowPayload(), version: 4 };
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyGet.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/workflows/wpid_1" ? { data: created } : historyResponse,
      ),
    );
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    // The newer canonical is neither adopted - recovery never replaces the canvas - nor read
    // as evidence THIS Accept produced it: another writer's save looks identical, and a
    // proposal with no fingerprint carries less identity, not more.
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(onWorkflowUpdate).not.toHaveBeenCalledWith(
      created,
      expect.anything(),
    );
    expect(screen.queryByText("Accepted — saved to the workflow")).toBeNull();
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled"),
    ).toBe(false);
  });

  it("holds a block Generate requested during a pending Accept, then sends it", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    let resolveApply: (value: unknown) => void = () => {};
    cancelPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveApply = resolve;
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    await act(async () => {
      useCopilotActionStore
        .getState()
        .requestBuild({ blockLabel: "extract_titles", prompt: "read titles" });
    });
    const generateSent = () =>
      streamCalls.some((call) => call.body.message.includes("extract_titles"));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    // A turn started now could stage a proposal the pending Accept then clears.
    expect(generateSent()).toBe(false);

    await act(async () => {
      resolveApply({ data: proposedWorkflowPayload() });
    });
    await waitFor(() => expect(generateSent()).toBe(true));
  });

  it("ignores a reload retry that lands after a newer turn in the same chat", async () => {
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    await submit("also grab the story scores");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    historyGet.mockRejectedValueOnce(new Error("network down"));
    await act(async () => {
      streamCalls[1]!.onMessage(
        plainReplyResponse("I'll fold that into the draft above."),
      );
      streamCalls[1]!.resolve();
    });
    expect(await screen.findByRole("alert")).toBeTruthy();
    // The held reload will answer with a row that no longer has a proposal.
    let finishOldReload: () => void = () => {};
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishOldReload = () => resolve(historyResponse);
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });

    await submit("rebuild it from scratch");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(3));
    await act(async () => {
      streamCalls[2]!.onMessage(
        proposalResponse("Rebuilt draft ready.", {
          turn_id: "turn-3",
          narrative_payload: proposalNarrativePayload({ turnId: "turn-3" }),
        }),
      );
      streamCalls[2]!.resolve();
    });
    await act(async () => {
      finishOldReload();
    });

    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
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

  it("keeps Turn off behind the accept's row write", async () => {
    // CHANGED BY THE #17099 MERGE. This asserted the same ordering against the CLIENT-SIDE
    // FALLBACK's row write (a v1 proposal whose apply 500s, applied locally and cleared by the
    // client). Slice 1 removes that fallback, so the vehicle is gone - but the concern is not:
    // the apply POST itself carries `auto_accept`, so it is still a write that can overwrite a
    // Turn off sent while it is in flight. Re-pointed at that write; the ordering it guards, and
    // the acceptsInFlight chain that enforces it, are unchanged.
    historyResponse.data.auto_accept = true;
    await renderChat();
    await submit("add a step");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    let finishRowWrite: (value: unknown) => void = () => {};
    cancelPost.mockImplementation((path: string) => {
      if (path === "/workflow/copilot/apply-proposed-workflow") {
        return new Promise((resolve) => (finishRowWrite = resolve));
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
      finishRowWrite({
        data: proposedWorkflowPayload({ workflow_id: "wf_saved" }),
      });
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

  // The terminal pass is what stops "Confirming…" sitting there ONCE IT IS REACHED, and it gets
  // there by ignoring the claim. Reaching it is a separate problem, pinned by `does not re-arm
  // against its own expired deadline when a superseded pass settles first` and the two `re-arms
  // for a Try again ...` tests: a gate replaced after the last re-arm ran gets no timer unless
  // the effect is keyed on the gate itself.
  //
  // CHECK THE NEXT TWO PARAGRAPHS AGAINST THE CODE BEFORE RELYING ON THEM; the PR body records
  // their history. No running count here on purpose - a number you must maintain is a claim you
  // must re-verify every round, and this one went stale twice.
  //
  // `keeps Save held when another writer holds a fresh claim at the terminal pass` and `still
  // holds Save at the terminal pass when a fresh claim has no surviving proposal` share the fresh
  // 300-second claim but are NOT a minimal pair: the first adds `proposed_workflow` AND
  // `proposed_workflow_metadata`, the second adds neither. They are not separated by MECHANISM
  // either. Both rows REPORT a lease, and `claimHoldDeadline` returns a deadline from that number
  // without ever consulting metadata - its metadata branch is reached only when the field is
  // ABSENT. So the renewed-claim hold is the same in both, and the second test holds by three
  // independent routes, staying green under mutations the first catches.
  //
  // The pair is not minimal BECAUSE THE MINIMAL PAIR DOES NOT EXIST. The obvious repair is to
  // make the two differ in one field; that is impossible rather than merely unwanted, so do not
  // add a third test for the missing corner. A CHAT-HISTORY ROW never reports metadata with a
  // null proposal - the scope that matters, because these tests drive history rows. There is
  // exactly ONE history producer: `WorkflowCopilotChatHistoryResponse` is constructed in a single
  // place, fed by `_history_proposal_state`, which returns non-null metadata on exactly one of its
  // four paths - and that path returns the proposal beside it. The other two producers of the
  // field are STREAM frames, which is why the stream is NOT so constrained: each takes metadata
  // from the STORED proposal while sending this turn's `updated_workflow`, so a reply-turn frame
  // can carry metadata beside a null workflow. The canonical-moved branch is the real shape of "a
  // fresh claim with no surviving proposal" - it reports the claim while returning no metadata,
  // deliberately, because hiding a live claim behind a hidden proposal is the lie that branch
  // exists to avoid. That is the state the second test covers.
  //
  // `releases Save at the terminal pass when an older instance omits the claim lease, which is
  // deliberate` reports the accepting claim WITHOUT its lease and asserts a release. Absent and
  // null are different answers there, and collapsing them into "no claim" is the coalescing
  // `claimHoldDeadline` exists to prevent.
  const armTerminalPassWithRenewedClaim = async () => {
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_claim_expires_in_seconds = 0.4;
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    // Held BEFORE the terminal pass, so a later release is that pass and not something earlier.
    await waitFor(() => expect(saveHeld()).toBe(true));
    historyResponse.data.proposed_claim_expires_in_seconds = 300;
    return saveHeld;
  };

  it("ignores a superseded reconciliation response instead of releasing Save under a live claim", async () => {
    // Two reads can be in flight at once: the scheduled re-check and a manual Try again, which
    // stays enabled while that read is out. Neither call checked whether a newer one had already
    // answered, so arrival order alone decided the gate - a stale row landing last could report
    // "Nothing was saved" and release Save while another writer was mid-write. That is the silent
    // release this branch exists to prevent, reached from inside rather than from the server.
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    // A short lease, so the scheduled pass is the terminal one and fires in hundreds of
    // milliseconds rather than at HOLD_RECHECK_MS (10s).
    historyResponse.data.proposed_claim_expires_in_seconds = 0.4;
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByText("Confirming…")).toBeTruthy();
    expect(saveHeld()).toBe(true);

    // The scheduled re-check fires and is held open mid-flight.
    let releaseScheduled: () => void = () => {};
    let scheduledCaptured = false;
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          scheduledCaptured = true;
          releaseScheduled = () =>
            resolve({
              data: {
                ...historyResponse.data,
                // A stale view: the claim looks gone and the proposal survives, which reads as
                // "nothing was saved" and would release the gate.
                proposed_workflow: proposedWorkflowPayload(),
                proposed_claim_expires_in_seconds: null,
              },
            });
        }),
    );
    const callsBeforeWait = historyGet.mock.calls.length;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 900));
    });
    // SETUP ASSERTIONS: prove the scheduled read actually fired and is held open. Without these
    // a green result cannot distinguish "no race" from "the probe never created one".
    expect(historyGet.mock.calls.length).toBeGreaterThan(callsBeforeWait);
    expect(scheduledCaptured).toBe(true);

    // Try again is still enabled during that read, and its own read sees a live claim. A short
    // lease so the gate it establishes has a reachable next deadline.
    historyResponse.data.proposed_claim_expires_in_seconds = 1.5;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    await waitFor(() => expect(saveHeld()).toBe(true));

    // The superseded scheduled response lands last. It must not overwrite what the newer read
    // established.
    await act(async () => {
      releaseScheduled();
    });
    // Assert the GATE, not only Save: `staleCanvas` can hold Save independently, so a Save-only
    // assertion would pass even if the superseded response had overwritten the gate.
    expect(screen.queryByText(/Nothing was saved/i)).toBeNull();
    expect(screen.queryByText(/Proposal changed/i)).toBeNull();
    expect(saveHeld()).toBe(true);

    // And the newer gate still has a TIMER. A terminal pass does not normally re-arm, so a
    // superseded one has to re-arm for the gate that replaced it - otherwise "Confirming…" sits
    // here with nothing scheduled and only a click ends it, which is the strand this branch
    // removes everywhere else. Asserting the instant after the stale response lands cannot see
    // that; only running past the next deadline can.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2600));
    });
    await waitFor(() =>
      expect(screen.queryByText("Confirming\u2026")).toBeNull(),
    );
  });

  it("does not re-arm against its own expired deadline when a superseded pass settles first", async () => {
    // The reverse-order case for `ignores a superseded reconciliation response instead of
    // releasing Save under a live claim`: the superseded response settles while the manual
    // read is still out. It pins that no request is issued in that window - re-arming there would
    // supersede a read that has not answered yet.
    //
    // It also separates the guarded re-arm from an UNCONDITIONAL one - but only because the wait
    // below sits in its OWN act() scope. A re-arm's re-render lands when the releasing scope
    // exits, so a wait sharing that scope passes through a window in which no re-arm timer exists
    // yet and can never observe the spurious read. That is why three earlier attempts here failed
    // to discriminate: a harness artefact, not a property of the code under test.
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    historyResponse.data.proposed_claim_expires_in_seconds = 0.4;
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByText("Confirming…")).toBeTruthy();

    // Hold the scheduled terminal read open.
    let releaseScheduled: () => void = () => {};
    let scheduledCaptured = false;
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          scheduledCaptured = true;
          releaseScheduled = () =>
            resolve({
              data: {
                ...historyResponse.data,
                proposed_workflow: proposedWorkflowPayload(),
                proposed_claim_expires_in_seconds: null,
              },
            });
        }),
    );
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 900));
    });
    expect(scheduledCaptured).toBe(true);

    // Start the manual read and hold it too, so the two can be released in a chosen order.
    let releaseManual: () => void = () => {};
    let manualCaptured = false;
    historyResponse.data.proposed_claim_expires_in_seconds = 1.5;
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          manualCaptured = true;
          releaseManual = () => resolve({ data: { ...historyResponse.data } });
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    expect(manualCaptured).toBe(true);

    // The stale one settles FIRST, while the manual read is still out.
    const readsBeforeRelease = historyGet.mock.calls.length;
    await act(async () => {
      releaseScheduled();
    });
    // Separate scope, deliberately: see the note at the top of this test. The spurious re-arm is
    // scheduled rather than synchronous, so the timer needs a window to fire in a scope where it
    // actually exists before asserting that it did not.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 250));
    });
    // No request may be issued here: the gate still carries this pass's own expired deadline, so
    // re-arming now would supersede the manual read that has not answered yet.
    expect(historyGet.mock.calls.length).toBe(readsBeforeRelease);

    await act(async () => {
      releaseManual();
    });
  });

  it("re-arms for a Try again that inherited this pass's own deadline", async () => {
    // The gate a Try again installs carries the SAME `holdExpiresAt` by design - `retryGateFailure`
    // hands the old deadline back as `holdUntil`, and a read that finds no claim, finds no proposal
    // or fails outright returns it unchanged. So "the deadline moved" is not the same proposition as
    // "the gate was replaced", and a re-arm keyed on the deadline never fires for the one
    // replacement the user performs by hand: the superseded pass sees equal deadlines and skips the
    // bump, the effect sees an unchanged dependency and never re-runs, and nothing is left to end
    // "Confirming…" but another click - which does not exit either, because it reproduces this
    // exact state. `ignores a superseded reconciliation response instead of releasing Save under
    // a live claim` passes only because its Try again reads a LIVE CLAIM,
    // which mints a different deadline.
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    historyResponse.data.proposed_claim_expires_in_seconds = 0.4;
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByText("Confirming…")).toBeTruthy();

    // Hold the scheduled pass open so its timer is spent and a read is in flight.
    let releaseScheduled: () => void = () => {};
    let scheduledCaptured = false;
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          scheduledCaptured = true;
          releaseScheduled = () =>
            resolve({
              data: {
                ...historyResponse.data,
                proposed_workflow: proposedWorkflowPayload(),
                proposed_claim_expires_in_seconds: null,
              },
            });
        }),
    );
    const callsBeforeWait = historyGet.mock.calls.length;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 900));
    });
    // SETUP ASSERTIONS: without these a green result cannot tell "re-armed correctly" from "the
    // probe never spent the original timer", which is the only thing making the gate depend on a
    // re-arm at all.
    expect(historyGet.mock.calls.length).toBeGreaterThan(callsBeforeWait);
    expect(scheduledCaptured).toBe(true);

    // Try again, and its read FAILS - the route to `unreadable()`, which returns the inherited
    // deadline unchanged. This is the step the sibling test does differently.
    historyGet.mockRejectedValueOnce(new Error("Network Error"));
    const callsBeforeRetry = historyGet.mock.calls.length;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    // SETUP ASSERTIONS: the retry read actually fired, and it left the gate still holding. A
    // green result would otherwise be consistent with the click doing nothing at all.
    expect(historyGet.mock.calls.length).toBeGreaterThan(callsBeforeRetry);
    expect(screen.queryByText("Confirming…")).not.toBeNull();

    // The superseded pass lands last and must re-arm for the gate that replaced it.
    historyGet.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      releaseScheduled();
    });

    // Assert the GATE reaches a terminal state on its own. Nothing is clicked here: if the re-arm
    // is keyed on the deadline, no timer exists and "Confirming…" sits indefinitely.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1500));
    });
    await waitFor(() => expect(screen.queryByText("Confirming…")).toBeNull());
  });

  it("re-arms for a Try again whose gate lands after the superseded pass settled", async () => {
    // STALE-FIRST. `re-arms for a Try again that inherited this pass's own deadline` releases the
    // superseded pass AFTER Try again has already
    // installed its gate, so the `.finally` sees a replacement and bumps. In THIS order the stale
    // read settles while Try again's read is still out - so the gate is still the one that pass was
    // serving, the `.finally` correctly declines to bump, and the replacement lands LATER, when Try
    // again's own read resolves. By then the only re-arm path has already run. Nothing else can
    // notice the new gate unless the effect is keyed on the gate ITSELF: its deadline is unchanged
    // by design. This is the order that refuted "a recover gate always has either a pending timer
    // or an in-flight read".
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    historyResponse.data.proposed_claim_expires_in_seconds = 0.4;
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByText("Confirming…")).toBeTruthy();

    // The scheduled terminal pass fires and is held open.
    let releaseScheduled: () => void = () => {};
    let scheduledCaptured = false;
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          scheduledCaptured = true;
          releaseScheduled = () =>
            resolve({
              data: {
                ...historyResponse.data,
                proposed_workflow: proposedWorkflowPayload(),
                proposed_claim_expires_in_seconds: null,
              },
            });
        }),
    );
    const callsBeforeWait = historyGet.mock.calls.length;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 900));
    });
    expect(historyGet.mock.calls.length).toBeGreaterThan(callsBeforeWait);
    expect(scheduledCaptured).toBe(true);

    // Try again, held open too, so the two can be released in a chosen order. Its read REJECTS,
    // which is the route to `unreadable()` and so to a gate carrying the inherited deadline.
    let rejectManual: () => void = () => {};
    let manualCaptured = false;
    historyGet.mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          manualCaptured = true;
          rejectManual = () => reject(new Error("Network Error"));
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    expect(manualCaptured).toBe(true);

    // The stale pass settles FIRST, while Try again is still out. Its `.finally` must NOT bump:
    // the gate it served is still the installed one.
    const readsBeforeStale = historyGet.mock.calls.length;
    await act(async () => {
      releaseScheduled();
    });
    // The window has to be its OWN act(): a re-arm's re-render lands when the releasing scope
    // exits, so counting reads immediately after it sees a tick in which no timer existed yet.
    // Without this the assertion below cannot detect a re-arm at all - it would be a vacuity
    // check that is itself vacuous.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 250));
    });
    // SETUP ASSERTION, and it is the precondition the whole test rests on: the stale pass issued
    // no re-arm here. If it had, the gate would already have a timer and the exit below would
    // prove nothing about a gate installed afterwards.
    expect(historyGet.mock.calls.length).toBe(readsBeforeStale);
    expect(screen.queryByText("Confirming…")).not.toBeNull();

    // Only now does Try again install its gate - after the last re-arm opportunity has passed.
    historyGet.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      rejectManual();
    });

    // Nothing is clicked from here. The gate must still reach a terminal state on its own.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1500));
    });
    await waitFor(() => expect(screen.queryByText("Confirming…")).toBeNull());
  });

  it("locks the pending question card while an Accept is unresolved", async () => {
    // `handleQuestionAnswer` is fenced and returns false SILENTLY - no request, no toast. So a card
    // that stays clickable under an unresolved Accept reports a submit that never happened, which
    // is the false receipt this whole slice exists to remove, reached without the server at all.
    // The Cancel control beside it already gates on the same fence; this card was the outlier.
    historyResponse.data.question_interactions = [
      {
        interaction_id: "q-1",
        turn_id: "turn-1",
        tool_call_id: "ask-1",
        status: "pending",
        response: null,
        created_at: "2026-09-17T00:00:01Z",
        resolved_at: null,
        parts: [
          {
            part_id: "p-1",
            prompt: "Which one?",
            choices: [{ choice_id: "a", text: "Column A" }],
          },
        ],
      },
    ];
    // The proposal arrives by HYDRATION rather than the composer: a question card renders its own
    // textarea, and the shared `submit` helper resolves a single textbox by role.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: null,
    };
    await renderChat();

    // SETUP ASSERTION: the card is actionable BEFORE the fence closes. Without this the assertion
    // below passes for a card that was never enabled, or never rendered at all.
    const skip = await screen.findByRole("button", { name: "Skip" });
    expect(skip.matches(":disabled")).toBe(false);

    historyResponse.data.proposed_claim_expires_in_seconds = 300;
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByText("Confirming\u2026")).toBeTruthy();

    // Both controls inert. Send is scoped to the card's own action row - the composer has a Send
    // too, and an unscoped query would assert against whichever came first.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Skip" }).matches(":disabled"),
      ).toBe(true),
    );
    const actionRow = screen.getByRole("button", { name: "Skip" })
      .parentElement!.parentElement!;
    expect(
      within(actionRow)
        .getByRole("button", { name: "Send" })
        .matches(":disabled"),
    ).toBe(true);
    // And the hold names itself where the choice count used to be, so the card is distinguishable
    // from a broken one.
    expect(within(actionRow).queryByText(/choices selected/)).toBeNull();
  });

  it("keeps the stale-canvas hold through a saved-gate retry that never refreshed the canvas", async () => {
    // THE COMPOUND CASE, and the only one the `fresh` narrowing changes. It is NOT the path Codex
    // filed: that one never arms `staleCanvas` at all, because both producers end in kind
    // "reload" and the saved gate is reached only when an apply 200s and the editor throws. Here
    // an EARLIER recovery arms the flag, and a LATER saved-gate retry used to clear it - applying
    // a workflow captured before that recovery, which says nothing about whether this canvas is
    // current. `persisted` was carrying that claim; only `fresh` should.
    //
    // Asserted at the user-visible outcome rather than the flag: during the saved gate
    // `acceptHoldReason` short-circuits the staleCanvas branch of `saveHoldReason`, so the flag is
    // unobservable until the retry clears the gate and the branch goes live again.
    let editorAccepts = true;
    const onWorkflowUpdate = vi.fn(() => {
      if (!editorAccepts) {
        throw new Error("editor could not load it");
      }
    });
    await renderChat({ onWorkflowUpdate });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");

    // 1. Arm staleCanvas the only way anything does: recovery giving up into `reload`.
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    historyResponse.data.proposed_workflow = null;
    leaseDecrementingFrom(0.4);
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(
      await screen.findByText(/Couldn't reload this proposal/, undefined, {
        timeout: 4000,
      }),
    ).toBeTruthy();
    expect(saveHeld()).toBe(true);

    // 2. A new turn. Sending drops the gate card; the canvas fact outlives it.
    leaseDecrementingFrom(null);
    await submit("another change");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    await act(async () => {
      streamCalls[1]!.onMessage(proposalResponse("Second draft."));
      streamCalls[1]!.resolve();
    });
    expect(screen.queryByText(/Couldn't reload this proposal/)).toBeNull();
    // SETUP ASSERTION: the hold must still be up here, or step 4 proves nothing about it.
    expect(saveHeld()).toBe(true);

    // 3. Accept: the server saves it and the editor refuses it. That is the `saved` gate.
    const savedWorkflow = proposedWorkflowPayload({
      workflow_id: "wf_saved",
    }) as unknown as WorkflowApiResponse;
    editorAccepts = false;
    cancelPost.mockResolvedValueOnce({ data: savedWorkflow });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByText("Saved, not shown")).toBeTruthy();

    // 4. The retry succeeds and clears the gate. It installed a workflow captured in step 3, so
    // it has refreshed nothing: the hold from step 1 must survive it.
    editorAccepts = true;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    await waitFor(() =>
      expect(screen.queryByText("Saved, not shown")).toBeNull(),
    );
    expect(saveHeld()).toBe(true);
  });

  it("locks the continuation cards while an Accept is unresolved", async () => {
    // Same class as the question card, one hop further: `handleSend` returns silently when the
    // fence is up, so a Confirm click under an unresolved Accept reports an action that never
    // happened - a false receipt with no server involved. Reported by Codex and by a human
    // reviewer independently. The human also described the account picker as greying out for the
    // turn; that half does not hold, because `handleSend`'s early return RESOLVES and the
    // `.finally` clears the latch. The silent no-op is the real defect.
    // The pending proposal arrives by HYDRATION so a later reply turn cannot clear it - the
    // coexistence the finding describes: an older proposal still pending while the newest turn
    // ends in a confirmation request.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "review_untested",
      workflow_run_id: null,
    };
    await renderChat();
    await submit("and one more thing");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(
        plainReplyResponse("Ready - confirm and I'll apply the change.", {
          updated_workflow:
            proposedWorkflowPayload() as unknown as WorkflowApiResponse,
          proposal_disposition: "review_untested",
        }),
      );
      streamCalls[0]!.resolve();
    });

    // SETUP ASSERTION: actionable BEFORE the fence closes, so a green below cannot come from a
    // card that never rendered or was always inert.
    const confirm = await screen.findByRole("button", { name: "Confirm" });
    expect(confirm.matches(":disabled")).toBe(false);

    historyResponse.data.proposed_claim_expires_in_seconds = 300;
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByText("Confirming\u2026")).toBeTruthy();

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Confirm" }).matches(":disabled"),
      ).toBe(true),
    );
    // And the hold names itself on the card, not only in a title a disabled button may never fire.
    expect(
      screen.getAllByText(/Copilot is checking|may have saved/i).length,
    ).toBeGreaterThan(0);
  });

  it("names the unsaved canvas edits that a saved-gate retry would discard", async () => {
    // `saved`'s Try again REPLACES the canvas with the workflow the server confirmed. The card
    // already said "this replaces what's on the canvas" generically; it said the same thing when
    // there was nothing to lose and when there was. The editor already tracks the difference -
    // `hasChanges`, guarded by beginInternalUpdate so our own applies do not set it - and
    // `reconcileCanonicalWorkflow` already refuses to overwrite on exactly that signal. This card
    // names the cost while the work still exists.
    const onWorkflowUpdate = vi.fn(() => {
      throw new Error("editor could not load it");
    });
    await renderChat({ onWorkflowUpdate });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    const savedWorkflow = proposedWorkflowPayload({
      workflow_id: "wf_saved",
    }) as unknown as WorkflowApiResponse;
    cancelPost.mockResolvedValueOnce({ data: savedWorkflow });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    expect(await screen.findByText("Saved, not shown")).toBeTruthy();

    // NEGATIVE ARM: a clean canvas has nothing to lose, so the card must not invent a cost.
    await act(async () => {
      useWorkflowHasChangesStore.setState({ hasChanges: false });
    });
    // Scoped to the ADDED sentence: the card's base copy already ends with "...which discards
    // unsaved canvas edits", so a loose match here would assert against the wrong text.
    expect(screen.queryByText(/canvas has unsaved changes/i)).toBeNull();

    // POSITIVE ARM: the user repaired something under the fence. Now it has to say so.
    await act(async () => {
      useWorkflowHasChangesStore.setState({ hasChanges: true });
    });
    expect(await screen.findByText(/canvas has unsaved changes/i)).toBeTruthy();
  });

  it("does not let a stale reload retry resurrect the card a newer one cleared", async () => {
    // Try again on a `reload` with no attempt sets no `isAccepting` and disables nothing, so two
    // of these reads can be in flight. Only call identity separates them: the chat id and send
    // epoch this path already checks are identical across a double-click. Same guard, and the same
    // reason, as `reconcileFailedAccept`.
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    // A follow-up turn with no new draft re-reads the row for the bypassed proposal; that read
    // fails, which is the only way this gate is reached.
    historyGet.mockRejectedValueOnce(new Error("network down"));
    await submit("also grab the story scores");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    await act(async () => {
      streamCalls[1]!.onMessage(
        plainReplyResponse("I'll fold that into the draft above."),
      );
      streamCalls[1]!.resolve();
    });
    expect(screen.queryByText("Couldn't reload")).toBeTruthy();
    expect(saveHeld()).toBe(true);

    // Two retries, both held open, released newest-first.
    let failOlder: () => void = () => {};
    let succeedNewer: () => void = () => {};
    historyGet.mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          failOlder = () => reject(new Error("older read lost"));
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });
    // The newer read SUCCEEDS and the proposal survives it. That matters: the reload card needs a
    // subject, so if the proposal were cleared here the card could not render whether or not the
    // stale catch restored the gate - and the assertion below would pass for the wrong reason.
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          succeedNewer = () =>
            resolve({
              data: {
                ...historyResponse.data,
                proposed_workflow: proposedWorkflowPayload(),
              },
            });
        }),
    );
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });

    // SETUP ASSERTION: both reads really are in flight, or the ordering below proves nothing.
    expect(typeof failOlder).toBe("function");
    expect(typeof succeedNewer).toBe("function");

    await act(async () => {
      succeedNewer();
    });
    await waitFor(() =>
      expect(screen.queryByText("Couldn't reload")).toBeNull(),
    );

    // The older read now fails. Its catch must not restore a gate it no longer owns - that would
    // report a failure for a reload the user watched succeed, and hold Save behind it.
    await act(async () => {
      failOlder();
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 100));
    });
    expect(screen.queryByText("Couldn't reload")).toBeNull();
    expect(saveHeld()).toBe(false);
  });

  const runOutTerminalPass = async () => {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1500));
    });
  };

  it("keeps Save held when another writer holds a fresh claim at the terminal pass", async () => {
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = await armTerminalPassWithRenewedClaim();
    // Another writer took a full lease. Its proposal is still there because canonical has not
    // moved YET - that writer is mid-write, which is precisely when Save must not be released.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "accepting",
      workflow_run_id: null,
    };
    await runOutTerminalPass();
    expect(saveHeld()).toBe(true);
  });

  it("releases Save at the terminal pass when an older instance omits the claim lease, which is deliberate", async () => {
    // The terminal-pass hold compares the REPORTED remainder, because at this pass our own
    // residual and another writer's renewal both compute to the same deadline and only the
    // reported number tells them apart. An instance predating that field reports nothing, so the
    // hold cannot fire and Save is released - a takeover claim goes unseen for the length of the
    // backend rollout. Declined rather than overlooked: holding on any omitted-field `accepting`
    // would also hold Save over the leftover row a crashed Accept leaves behind, which is a likely
    // state for a recovery to be in, and that is the strand this branch exists to remove.
    //
    // The remedy that avoids that cost is claim IDENTITY - comparing `claimed_at` rather than
    // doing clock arithmetic on a remainder. The client type does not declare that field today.
    // THIS PIN RETIRES once every instance reports the lease.
    //
    // Pinned so a change that makes this guard fire on an absent value fails here rather than
    // silently. Its control is `keeps Save held when another writer holds a fresh claim at the
    // terminal pass`, identical but for the field being reported.
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = await armTerminalPassWithRenewedClaim();
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "accepting",
      workflow_run_id: null,
    };
    // The only difference from the control: an older instance cannot report the lease.
    historyResponse.data.proposed_claim_expires_in_seconds = undefined;
    await runOutTerminalPass();
    expect(saveHeld()).toBe(false);
  });

  it("still holds Save at the terminal pass when a fresh claim has no surviving proposal", async () => {
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = await armTerminalPassWithRenewedClaim();
    await runOutTerminalPass();
    expect(saveHeld()).toBe(true);
  });

  // Accept with no chat id yet: it resolves one mid-flight, and the ref that names it is
  // assigned by a passive effect, so the ref still reads empty for the rest of the handler.
  // `reconciles a failed Accept against the chat id it just resolved` and `holds the fence when
  // the chat row read comes back empty` both drive that window; they differ in what the row finds,
  // because
  // that is the only thing that tells the two halves of this fix apart.
  const acceptWithChatIdResolvedMidFlight = async () => {
    historyResponse.data.workflow_copilot_chat_id = null;
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Draft ready.", {
          workflow_copilot_chat_id: undefined,
        }),
      );
      streamCalls[0]!.resolve();
    });
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
    historyResponse.data.workflow_copilot_chat_id = "chat-1";
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
  };

  const saveIsHeld = () =>
    screen.getByRole("button", { name: /^Save workflow/ }).matches(":disabled");

  it("reconciles a failed Accept against the chat id it just resolved", async () => {
    await acceptWithChatIdResolvedMidFlight();
    // The row still carries the proposal, so reading it settles the outcome DEFINITELY.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    // Reading the row requires the id this handler is holding; the ref is still empty. Without
    // it the read is skipped and the gate can only say "may have saved" - so this asserts the
    // DEFINITE verdict, not merely that some fence exists.
    expect(await screen.findByText("Not saved")).toBeTruthy();
    expect(screen.queryByText("Confirming\u2026")).toBeNull();
  });

  it("holds the fence when the chat row read comes back empty", async () => {
    await acceptWithChatIdResolvedMidFlight();
    // Resolve the id, then answer the row read with no row at all.
    historyGet.mockImplementationOnce(() => Promise.resolve(historyResponse));
    historyGet.mockImplementationOnce(() => Promise.resolve({ data: null }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    // Nothing identified the outcome, and this handler cleared its own gate before the apply,
    // so it owes a replacement. Unknown may not release Save.
    await waitFor(() => expect(saveIsHeld()).toBe(true));
  });

  it("keeps Save held when a reload's own fence reaches its terminal pass", async () => {
    // A reload lands on a claim with almost no lease left, so the fence hydration arms schedules
    // a TERMINAL recheck immediately - the first one, with no earlier read behind it. That is
    // the only path to the terminal pass that reconciliation never touches.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "accepting",
      workflow_run_id: null,
    };
    historyResponse.data.proposed_claim_expires_in_seconds = 0.4;
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    await waitFor(() => expect(saveHeld()).toBe(true));

    // Another writer takes a fresh full lease before that terminal read runs.
    historyResponse.data.proposed_claim_expires_in_seconds = 300;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1500));
    });
    expect(saveHeld()).toBe(true);
  });

  it("holds Save when a terminal pass finds a live claim it has no baseline for", async () => {
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    // A failed Accept arms the fence against a claim of ours.
    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_claim_expires_in_seconds = 1.5;
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    await waitFor(() => expect(saveHeld()).toBe(true));

    // Try again finds NO claim: ours resolved, so the remembered remainder is gone with it.
    historyResponse.data.proposed_claim_expires_in_seconds = null;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });

    // Another writer then takes a lease before the terminal pass. It is SMALLER than the one
    // this fence first saw, so a comparison against a stale remainder would read it as our own
    // claim decaying and release Save over an active writer.
    //
    // The row carries METADATA on purpose: without it the claim is unattributable and the
    // WORKFLOW-level Save hold arms instead, which would hold Save for a different reason and
    // leave this test green whatever the renewal rule does. It has to be a claim the row can
    // tie to a proposal, so the only thing that can hold Save here is the renewal check.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "accepting",
      workflow_run_id: null,
    };
    historyResponse.data.proposed_claim_expires_in_seconds = 100;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2500));
    });
    expect(saveHeld()).toBe(true);
  });

  it("holds Save when a Reject-409 resync finds a claim it cannot attribute", async () => {
    // The unattributed-claim hold is a fact about the WORKFLOW, so it cannot live on the
    // hydration path alone: this row is learned through the resync a 409 drives, and another
    // writer holding the workflow is just as true when that is how we found out.
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    expect(saveHeld()).toBe(false);

    // A concurrent save moved canonical, so the proposal is withheld - but its claim is live,
    // and the server reports it independently now.
    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_workflow_metadata = null;
    historyResponse.data.proposed_claim_expires_in_seconds = 100;
    cancelPost.mockRejectedValueOnce({ response: { status: 409 } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    });

    await waitFor(() => expect(saveHeld()).toBe(true));
  });

  it("keeps the baseline a renewal is measured against when an ordinary poll sees one", async () => {
    // Every other renewal test enters at the TERMINAL pass. This one drives the renewal arriving
    // on an ORDINARY re-check, which re-arms the fence - and a re-arm that adopted the new
    // reading as its baseline would leave the terminal pass comparing that writer's own lease
    // against itself, finding it smaller as it decays, and releasing Save under an active write.
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_claim_expires_in_seconds = 1.2;
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    await waitFor(() => expect(saveHeld()).toBe(true));

    // An ordinary re-check finds a LARGER remainder: our claim lapsed and another writer took
    // one. Try again is exactly that re-check - it re-arms the fence without being terminal.
    historyResponse.data.proposed_claim_expires_in_seconds = 2;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });

    // The terminal pass then sees that writer's lease part-way through decaying, with its
    // proposal still visible because canonical has not moved yet.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "accepting",
      workflow_run_id: null,
    };
    historyResponse.data.proposed_claim_expires_in_seconds = 1.6;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 3000));
    });
    expect(saveHeld()).toBe(true);
  }, 15000);

  it("holds when a claim reappears after a read that reported none", async () => {
    // A read reporting NO claim is positive evidence that the lease ended - not absence of
    // information. Any claim seen afterwards was taken later, so with ABSOLUTE expiries it
    // cannot be the old one decaying: a lease is a fixed length added to claimed_at, so a newer
    // claim always expires later. A RELATIVE remainder cannot express that - a smaller number
    // read later looks like decay - which is why this sequence releases Save under it.
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    historyResponse.data.proposed_workflow = null;
    const oursEndsAt = leaseDecrementingFrom(1.2);
    cancelPost.mockRejectedValueOnce(new Error("Network Error"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });
    await waitFor(() => expect(saveHeld()).toBe(true));

    // A re-check finds NO claim at all: ours is provably over.
    historyResponse.data.proposed_claim_expires_in_seconds = null;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    });

    const heldAfterNullRead = saveHeld();

    // Another writer then claims. Its remainder is SMALLER than the one this fence opened with,
    // so a relative comparison reads it as our own lease decaying.
    historyResponse.data.proposed_workflow = proposedWorkflowPayload();
    historyResponse.data.proposed_workflow_metadata = {
      owner_turn_id: "turn-1",
      revision: 1,
      canonical_fingerprint: "canonical-1",
      disposition: "accepting",
      workflow_run_id: null,
    };
    // A LEASE IS A FIXED LENGTH added to claimed_at, so a claim taken LATER always expires
    // later - even though its REMAINDER read at any instant is smaller than ours was at ours.
    // That is the whole of the distinction: 0.5s past our expiry, and a smaller remainder.
    leaseDecrementingFrom((oursEndsAt + 500 - Date.now()) / 1000);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2500));
    });
    expect(heldAfterNullRead).toBe(true);
    expect(saveHeld()).toBe(true);
  }, 15000);

  it("holds Save when mount hydration finds a claim it cannot attribute", async () => {
    // The THIRD row-learning site. Reconciliation and the Reject-409 resync are driven
    // elsewhere; this one is hydration, and removing its recordClaimState call left the whole
    // file green before this test existed.
    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_workflow_metadata = null;
    historyResponse.data.proposed_claim_expires_in_seconds = 100;
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    await waitFor(() =>
      expect(
        screen
          .getByRole("button", { name: /^Save workflow/ })
          .matches(":disabled"),
      ).toBe(true),
    );
  });

  it("REVIEW PROBE: holds Save after an ordinary no-claim poll then a smaller later claim", async () => {
    await renderChat();
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");

    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    // The production lease is minutes, not the sub-10-second value that makes
    // every re-check terminal. Fake timers also fake Date, which this probe
    // asserts below before trusting comparisons that use absolute instants.
    vi.useFakeTimers();
    try {
      const openedAt = Date.now();
      historyResponse.data.proposed_workflow = null;
      historyResponse.data.proposed_workflow_metadata = null;
      historyResponse.data.proposed_claim_expires_in_seconds = 180;
      cancelPost.mockRejectedValueOnce(new Error("Network Error"));
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Accept" }));
      });
      expect(saveHeld()).toBe(true);

      // The first timer pass is ordinary (170 seconds remain), and finds the
      // original claim gone. Its 10 seconds must advance Date.now too.
      historyResponse.data.proposed_claim_expires_in_seconds = null;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });
      expect(Date.now()).toBe(openedAt + 10_000);
      expect(saveHeld()).toBe(true);

      // A later writer now owns a shorter, still-live lease. The ordinary pass
      // re-arms a deadline from that claim, but must preserve safety after the
      // previous no-claim answer invalidated the opening claim's identity.
      historyResponse.data.proposed_workflow = proposedWorkflowPayload();
      historyResponse.data.proposed_workflow_metadata = {
        owner_turn_id: "turn-1",
        revision: 1,
        canonical_fingerprint: "canonical-1",
        disposition: "accepting",
        workflow_run_id: null,
      };
      historyResponse.data.proposed_claim_expires_in_seconds = 100;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });
      expect(saveHeld()).toBe(true);

      // Let ordinary polls see no claim until the final 10-second interval;
      // they keep the deadline established by the later writer's lease.
      historyResponse.data.proposed_workflow = null;
      historyResponse.data.proposed_workflow_metadata = null;
      historyResponse.data.proposed_claim_expires_in_seconds = null;
      for (let poll = 0; poll < 9; poll += 1) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(10_000);
        });
      }
      expect(saveHeld()).toBe(true);

      // The terminal read finds the same later live claim, now shorter than
      // the opening 180-second remainder. Save must remain held.
      historyResponse.data.proposed_workflow = proposedWorkflowPayload();
      historyResponse.data.proposed_workflow_metadata = {
        owner_turn_id: "turn-1",
        revision: 1,
        canonical_fingerprint: "canonical-1",
        disposition: "accepting",
        workflow_run_id: null,
      };
      historyResponse.data.proposed_claim_expires_in_seconds = 90;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(historyGet).toHaveBeenCalledTimes(14);
      expect(screen.getByText("Couldn't reload")).toBeTruthy();
      expect(saveHeld()).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  }, 15_000);

  // CALLBACK-LEVEL, deliberately. These drive onSelectChat directly, which is where the guard
  // resolves. Whether today's DOM lets a user reach the overlapping window is NOT established -
  // WorkflowCopilotHistory intends to disable and close once loadChatInPlace sets
  // isLoadingHistory, and the docked store is updated by a passive effect, so the window is
  // bounded by that effect. These assert the guard's resolution, not a user-reachable path.
  const selectChat = async (id: string) => {
    await act(async () => {
      await useCopilotHeaderStore.getState().controls?.onSelectChat?.({
        workflow_copilot_chat_id: id,
      } as Parameters<
        NonNullable<
          NonNullable<
            ReturnType<typeof useCopilotHeaderStore.getState>["controls"]
          >["onSelectChat"]
        >
      >[0]);
    });
  };

  const chatPayload = (id: string, text: string) => ({
    data: {
      ...historyResponse.data,
      workflow_copilot_chat_id: id,
      proposed_workflow: null,
      chat_history: [
        {
          sender: "ai",
          content: text,
          created_at: new Date().toISOString(),
        },
      ],
    },
  });

  it("lets the NEWER chat selection win when two loads overlap", async () => {
    await renderChat({ docked: true });
    let releaseSecond: ((value: unknown) => void) | null = null;
    let releaseThird: ((value: unknown) => void) | null = null;
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseSecond = resolve as (value: unknown) => void;
        }),
    );
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseThird = resolve as (value: unknown) => void;
        }),
    );

    await selectChat("chat-2");
    await selectChat("chat-3");
    expect(releaseSecond).not.toBeNull();
    expect(releaseThird).not.toBeNull();

    // The older load returns FIRST, then the newer one.
    await act(async () => {
      releaseSecond!(chatPayload("chat-2", "SECOND chat body"));
      await Promise.resolve();
    });
    await act(async () => {
      releaseThird!(chatPayload("chat-3", "THIRD chat body"));
      await Promise.resolve();
    });

    // The user's most recent selection is what they get.
    await waitFor(() =>
      expect(screen.queryByText("THIRD chat body")).not.toBeNull(),
    );
    expect(screen.queryByText("SECOND chat body")).toBeNull();
  });

  it("lets a re-selection of the CURRENT chat cancel a pending load", async () => {
    // The equality early return makes no second read, so an increment placed after it would
    // never run for this case - and this is the case where a user undoes a mis-click.
    await renderChat({ docked: true });
    let releaseOther: ((value: unknown) => void) | null = null;
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseOther = resolve as (value: unknown) => void;
        }),
    );

    await selectChat("chat-2");
    expect(releaseOther).not.toBeNull();
    await selectChat("chat-1");

    await act(async () => {
      releaseOther!(chatPayload("chat-2", "OTHER chat body"));
      await Promise.resolve();
    });
    expect(screen.queryByText("OTHER chat body")).toBeNull();
  });

  it("attaches the auto-commit saved gate to the committing turn, not the bypassed one", async () => {
    // From reviewer 1677. POSITIVE assertion on purpose: "the status is not inside turn 1's gate"
    // passes when the element is absent, when the render changed shape, and when turn 2 never
    // rendered a gate at all - an earlier attempt of mine did exactly that and pinned nothing.
    // Walking up from the status node and naming the host gate says what IS true.
    //
    // PARTIAL PIN, and the limit is worth knowing: it fails when the turn-id handle survives, and
    // it also passes when only `setProposedWorkflow(null)` is dropped. It pins the gate's
    // ATTACHMENT, not the staged draft handle.
    const onWorkflowUpdate = vi.fn((workflow: { workflow_id?: string }) => {
      if (workflow?.workflow_id === "wf_turn2")
        throw new Error("editor could not load it");
    });
    await renderChat({
      onWorkflowUpdate:
        onWorkflowUpdate as unknown as ChatProps["onWorkflowUpdate"],
    });
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    await submit("now change it again");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(2));
    await act(async () => {
      streamCalls[1]!.onMessage(
        proposalResponse("Committed.", {
          turn_id: "turn-2",
          proposal_disposition: "auto_applicable",
          workflow_applied: true,
          updated_workflow: proposedWorkflowPayload({
            workflow_id: "wf_turn2",
          }) as unknown as WorkflowApiResponse,
          narrative_payload: proposalNarrativePayload({
            turnId: "turn-2",
            turnIndex: 1,
            proposalDisposition: "auto_applicable",
          }),
        }),
      );
      streamCalls[1]!.resolve();
    });
    const status = await screen.findByText("Saved, not shown");
    expect(status.closest('[id^="copilot-gate-"]')?.id).toBe(
      "copilot-gate-turn-2",
    );
  });

  it("holds Save when the server committed a version the editor cannot take", async () => {
    // The auto-commit path ignored applyWorkflowUpdate's boolean: with `workflow_applied: true`
    // the server HAS written this version, so an editor that rejects it leaves the canvas older
    // than canonical - and clearing the proposal handles there released Save over it with
    // nothing on screen saying so. That is the state `saved` exists for, reached by auto-commit
    // instead of Accept, so it arms the same gate.
    const onWorkflowUpdate = vi.fn(() => {
      throw new Error("editor could not load it");
    });
    await renderChat({ onWorkflowUpdate });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");

    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(
        proposalResponse("Committed.", {
          proposal_disposition: "auto_applicable",
          workflow_applied: true,
        }),
      );
      streamCalls[0]!.resolve();
    });

    expect(await screen.findByText("Saved, not shown")).toBeTruthy();
    expect(saveHeld()).toBe(true);
  });

  it("keeps the chat's controls locked while a newer load is still in flight", async () => {
    // The selection epoch stops a SUPERSEDED response before it applies - but its `finally`
    // cleared the loading flag anyway, and that flag gates History, New chat and the gate's
    // action row. So the abandoned load unlocked the chat while the newer one was still running,
    // and an Accept started in that window races a response that will swap the chat underneath
    // it. The same shape was in the mount loader's `finally` too; both now release through
    // endHistoryLoad, which ignores a load that no longer owns the flag.
    //
    // Callback level, like `lets the NEWER chat selection win when two loads overlap` and `lets a
    // re-selection of the CURRENT chat cancel a pending load`: this asserts the guard's
    // resolution. DOM reachability of the overlapping window is bounded by the passive effect
    // that publishes these controls and is NOT established.
    await renderChat({ docked: true });
    let releaseOlder: ((value: unknown) => void) | null = null;
    let releaseNewer: ((value: unknown) => void) | null = null;
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseOlder = resolve as (value: unknown) => void;
        }),
    );
    historyGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          releaseNewer = resolve as (value: unknown) => void;
        }),
    );

    await selectChat("chat-2");
    await selectChat("chat-3");
    expect(releaseOlder).not.toBeNull();
    expect(releaseNewer).not.toBeNull();

    // The superseded load answers first and must not unlock anything.
    await act(async () => {
      releaseOlder!(chatPayload("chat-2", "SECOND chat body"));
      await Promise.resolve();
    });
    expect(useCopilotHeaderStore.getState().controls?.disabled).toBe(true);

    // The load that still owns the flag releases it, so this does not latch.
    await act(async () => {
      releaseNewer!(chatPayload("chat-3", "THIRD chat body"));
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(useCopilotHeaderStore.getState().controls?.disabled).toBe(false),
    );
  });

  it("keeps the workflow-claim hold when ANOTHER chat's row reports no claim", async () => {
    // A chat row is CHAT-SCOPED evidence for a WORKFLOW-SCOPED fact: the server reads the claim
    // off THAT chat's own proposal blob, so a second chat reports none while the first writer's
    // claim is still live. Retiring the hold on that answer releases Save under an active write,
    // which is the hold's entire purpose - existence needs one witness, absence needs coverage
    // no row has. Only the lease timer may end it.
    await renderChat({ docked: true });
    render(
      <TooltipProvider>
        <SaveButton />
      </TooltipProvider>,
    );
    const saveHeld = () =>
      screen
        .getByRole("button", { name: /^Save workflow/ })
        .matches(":disabled");
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });

    // Another writer holds this workflow and the server cannot tie the claim to a proposal.
    historyResponse.data.proposed_workflow = null;
    historyResponse.data.proposed_workflow_metadata = null;
    historyResponse.data.proposed_claim_expires_in_seconds = 100;
    cancelPost.mockRejectedValueOnce({ response: { status: 409 } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    });
    await waitFor(() => expect(saveHeld()).toBe(true));

    // History stays enabled under this hold by design, so the switch is reachable. Chat B has no
    // proposal of its own, so the server reports no claim FOR CHAT B - not for the workflow.
    historyGet.mockImplementationOnce(() =>
      Promise.resolve({
        data: {
          ...historyResponse.data,
          workflow_copilot_chat_id: "chat-2",
          proposed_workflow: null,
          proposed_workflow_metadata: null,
          proposed_claim_expires_in_seconds: null,
          chat_history: [
            {
              sender: "ai",
              content: "SECOND chat body",
              created_at: new Date().toISOString(),
            },
          ],
        },
      }),
    );
    await selectChat("chat-2");
    await waitFor(() =>
      expect(screen.queryByText("SECOND chat body")).not.toBeNull(),
    );
    expect(saveHeld()).toBe(true);
  });

  it("keeps the History lock reason reachable on the floating header", async () => {
    // The floating header is outside any TooltipProvider, so the reason is a native title -
    // and a DISABLED button receives no hover and takes no focus, so a title ON IT can never
    // be read. It has to hang off something that is not disabled.
    await renderChat();
    await submit("build me a workflow");
    await waitFor(() => expect(postStreaming).toHaveBeenCalledTimes(1));
    await act(async () => {
      streamCalls[0]!.onMessage(proposalResponse("Draft ready."));
      streamCalls[0]!.resolve();
    });
    // An Accept that never settles holds the fence, which is what locks History.
    cancelPost.mockImplementationOnce(() => new Promise(() => {}));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    });

    const history = screen.getByRole("button", { name: /^History/ });
    expect(history.matches(":disabled")).toBe(true);
    const explained = screen
      .getAllByTitle("Copilot is saving your accepted changes.")
      .find((el) => el.contains(history))!;
    expect(explained).toBeTruthy();
    // Whatever carries the reason must be able to receive a hover: not the disabled control.
    expect(explained.matches(":disabled")).toBe(false);
    expect(explained.contains(history)).toBe(true);
  });
});
