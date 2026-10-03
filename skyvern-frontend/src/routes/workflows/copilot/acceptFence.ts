import { type WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";

import {
  type CopilotProposalMetadata,
  type WorkflowCopilotChatHistoryResponse,
} from "./workflowCopilotTypes";

/**
 * The Accept fence's pure rules: whether the server may still be writing an Accept, how long the
 * fence covering it lasts, and what a hydrated chat row may do to it. They live outside the chat
 * component so the contract can be exercised directly.
 */

// `wroteNothing` is the apply route's own answer, not an inference. Required rather than
// optional so each construction site has to say what it knows.
export type AcceptAttempt = {
  alwaysAccept: boolean;
  token: string | null;
  wroteNothing: boolean;
};

// The only two statuses the apply route's refusal ladder raises, and it raises both before it
// creates the version, so either proves no write. Nothing wider qualifies: a transport failure
// carries no status at all, its 404 says the chat id did not resolve so a later read of that same
// id proves nothing, and a gateway or auth 4xx never reached the ladder.
export const applyWroteNothing = (status: number | undefined): boolean =>
  status === 400 || status === 409;

export const proposalTokenOf = (
  metadata: CopilotProposalMetadata | null,
): string | null =>
  metadata ? `${metadata.owner_turn_id}:${metadata.revision}` : null;

// How often the fence looks again while it lasts.
export const HOLD_RECHECK_MS = 10_000;

export type GateFailure =
  | ({ kind: "accept" } & AcceptAttempt)
  // Terminal: a refusal proved this Accept never wrote, so there is nothing to confirm or retry.
  // Its card copy is shared with a replaced proposal, and the card's `hasProposal` picks which.
  | ({ kind: "changed" } & AcceptAttempt)
  // claimExpiresAtSeen is the claim's ABSOLUTE EXPIRY as the read that OPENED this fence saw it,
  // carried on the gate so the comparison can only use a value belonging to this fence. Absolute,
  // because a remainder measured at two unknown times is not a discriminator at all.
  | ({
      kind: "recover";
      claimExpiresAtSeen: number | null;
    } & AcceptAttempt)
  // The one outcome the client KNOWS: the apply returned 200 and its workflow is in hand, but
  // the editor could not take it. Distinct from `recover` because all three of its behaviours
  // differ — the copy is "saved", not "may have saved"; the retry re-applies this workflow
  // rather than re-reading the chat row; and there is no server claim to expire, so no
  // deadline may release it over a canvas we know is stale.
  // ownerTurnId is carried here because hydration clears the component's pending-turn field
  // whenever the row has no proposal - which is the truth after a save - and this gate
  // deliberately outlives that. Without it the retry has no turn to mark accepted, so a save
  // that definitely landed reports nothing.
  | ({
      kind: "saved";
      savedWorkflow: WorkflowApiResponse;
      ownerTurnId: string | null;
    } & AcceptAttempt)
  | { kind: "reload"; attempt: AcceptAttempt | null };

// The one rule for "the server may still be writing this Accept", and the deadline the fence
// ends at. A reported remainder is the server's own; an absent field (not an explicit null) is
// an instance that predates it, which can only name the claim — and that claim can run for the
// whole lease, so it holds until `unreportedDeadline`.
export const claimHoldDeadline = (
  row: Pick<
    WorkflowCopilotChatHistoryResponse,
    "proposed_claim_expires_in_seconds" | "proposed_workflow_metadata"
  >,
  unreportedDeadline: number,
): number | null => {
  // Absent and null are different answers: null is a current server saying no claim is held,
  // absent is a server that cannot say. Coalescing them releases the fence mid-rollout.
  const reported = row.proposed_claim_expires_in_seconds;
  if (reported !== undefined) {
    return reported !== null && reported > 0
      ? Date.now() + reported * 1000
      : null;
  }
  return row.proposed_workflow_metadata?.disposition === "accepting"
    ? unreportedDeadline
    : null;
};

/**
 * What a hydrated chat row does to the Accept fence. It ARMS the hold when the row reports a
 * claim, and preserves the two holds a chat row cannot disprove:
 *
 * - `recover`, because "the server reports no claim" is not proof no write is in flight: the
 *   apply route acquires its claim only after its canonical lookup and normalization, so a read
 *   can overtake a POST that is about to write. Releasing is that hold's own job, via its deadline.
 * - `saved`, because that gate carries the ONLY copy of a workflow the server confirmed. Dropping
 *   it discards the retry's copy and releases Save over a canvas known to be older than the
 *   server - the exact overwrite this fence exists to prevent.
 *
 * It does not preserve `accept` or `reload`: `accept` says nothing was saved, which a fresh row
 * restates for itself, and `reload`'s Save hold lives in `staleCanvas`, which hydration never
 * touches. This is NOT "monotone, never disarms a hold" - that would be true only of `recover`
 * and would hide the `saved` case from a reader checking this function for something else.
 */
export const hydratedGateFailure = (
  row: Pick<
    WorkflowCopilotChatHistoryResponse,
    | "proposed_claim_expires_in_seconds"
    | "proposed_workflow_metadata"
    | "auto_accept"
  >,
  current: GateFailure | null,
  unreportedDeadline: number,
): GateFailure | null => {
  const holdExpiresAt = claimHoldDeadline(row, unreportedDeadline);
  // Only over a claim this row can TIE TO A PROPOSAL. A claim with no metadata is very likely
  // another writer's, and `recover` would tell the user their own Accept may have saved and send
  // them to a Try again that does not render. That hold lives in `unattributedClaimDeadline`.
  const claimIsAttributable = Boolean(row.proposed_workflow_metadata);
  // Not over a `saved` hold. That gate KNOWS the write landed - it holds the workflow the
  // server returned - which is strictly more information than a claim saying one MAY be in
  // flight, so arming here would trade certainty for uncertainty: it would drop the only copy
  // of that workflow and give the hold a deadline that can release over a stale canvas. The
  // claim can outlive the write, because the route clears it after the write, best-effort.
  if (
    holdExpiresAt !== null &&
    claimIsAttributable &&
    current?.kind !== "saved"
  ) {
    return {
      kind: "recover",
      alwaysAccept: row.auto_accept ?? false,
      token: proposalTokenOf(row.proposed_workflow_metadata ?? null),
      // A hydrated fence has no apply answer of its own: it was armed by a claim, not a refusal.
      wroteNothing: false,
      // Hydration RE-ARMS an open fence as readily as it opens one, so it may not move a
      // baseline either: an existing recovery hold keeps the remainder it opened with.
      claimExpiresAtSeen: fenceBaselineFor(
        current?.kind === "recover" ? current.claimExpiresAtSeen : undefined,
        holdExpiresAt,
      ),
    };
  }
  return current?.kind === "recover" || current?.kind === "saved"
    ? current
    : null;
};

/**
 * When the server reports a live claim it cannot tie to a displayable proposal. Someone is
 * writing this workflow and it is almost certainly not this turn, so Save must hold - but
 * nothing here is about OUR Accept, so it arms no gate and shows no card. Returns the deadline
 * the hold ends at, so the lock always ends rather than waiting for a hydration that may never
 * come.
 */
export const unattributedClaimDeadline = (
  row: Pick<
    WorkflowCopilotChatHistoryResponse,
    "proposed_claim_expires_in_seconds" | "proposed_workflow_metadata"
  >,
  now: number = Date.now(),
): number | null => {
  const reported = row.proposed_claim_expires_in_seconds;
  if (typeof reported !== "number" || reported <= 0) {
    return null;
  }
  return row.proposed_workflow_metadata ? null : now + reported * 1000;
};

/**
 * The instant a fence compares against, fixed AT ITS OPENING and never moved afterwards.
 *
 * `undefined` means no fence exists yet, so this read opens one and its reading becomes the
 * baseline. Anything else belongs to a fence already open and is returned untouched - INCLUDING
 * null, which is not "no answer yet" but the answer itself: a fence that opened blind must stay
 * blind, because the terminal pass treats a missing baseline as grounds to HOLD, and letting a
 * later poll supply one from ANOTHER writer's claim would hand that rule a number to compare
 * against and release. A fence may not become better informed than it was when it opened.
 */
export const fenceBaselineFor = (
  claimSeen: number | null | undefined,
  seenNow: number | null,
): number | null => (claimSeen === undefined ? seenNow : claimSeen);

/**
 * How a chat row may move the unattributed-claim hold: it may ARM it, it may EXTEND it, and it
 * may never RETIRE it.
 *
 * The evidence is narrower than the fact it maintains. "Another writer holds this workflow" is
 * workflow-scoped, but the server reads the claim off ONE CHAT'S proposal blob, so a row proves
 * only that SOME claim ends no earlier than it says - and a row reporting none covers its own
 * chat alone. Existence needs a single witness; absence needs coverage no row has.
 *
 * Hence MAX, NOT LAST. A chat-scoped read cannot tell you whose claim it is seeing, so the
 * latest deadline ever observed is the earliest instant it is safe to release; taking the last
 * would let a second chat's shorter claim release Save while the first writer is still writing.
 * The decaying reads of ONE lease all compute the same absolute instant, so max costs nothing
 * there - it holds longer only where two claims are genuinely indistinguishable, which is
 * exactly where holding is correct.
 */
export const extendedClaimHold = (
  current: number | null,
  deadline: number | null,
): number | null => {
  if (deadline === null) {
    return current;
  }
  return current === null || deadline > current ? deadline : current;
};

// These status/detail pairs are returned before the apply route writes a workflow.
export function definitiveAcceptRejection(error: unknown): string | null {
  const response = (
    error as {
      response?: { status?: number; data?: { detail?: unknown } };
    } | null
  )?.response;
  const detail = response?.data?.detail;
  if (typeof detail !== "string") return null;
  if (response?.status === 404 && detail === "Chat not found") return detail;
  if (
    response?.status === 400 &&
    (detail === "No proposed workflow to apply" ||
      detail === "Proposed workflow has no copilot YAML to apply" ||
      detail.startsWith("Proposed copilot YAML is invalid: "))
  )
    return detail;
  if (
    response?.status === 409 &&
    [
      "Copilot proposal metadata is invalid; reload required",
      "Workflow changed after this proposal",
    ].includes(detail)
  )
    return detail;
  return null;
}
