import { TurnNarrativeState } from "../narrativeState";

type DiffCardTitleOptions = {
  pendingProposal?: boolean;
  rejected?: boolean;
  accepted?: boolean;
};

export function getDiffCardTitle(
  turn: TurnNarrativeState,
  {
    pendingProposal = false,
    rejected = false,
    accepted = false,
  }: DiffCardTitleOptions = {},
): string {
  const summary = turn.draft?.summary?.trim();
  if (summary) {
    return summary;
  }

  if (accepted) {
    return "Applied changes";
  }

  // "Applied changes" requires the backend's explicit auto-applied signal.
  // Everything else - pending review, a rejected auto-applicable draft, or a
  // null/unknown disposition from a forward-compatible backend - defaults to
  // "Proposed changes" rather than assuming the change landed.
  if (
    rejected ||
    pendingProposal ||
    turn.cancelled ||
    turn.terminal === "error" ||
    turn.proposalDisposition !== "auto_applicable"
  ) {
    return "Proposed changes";
  }

  return "Applied changes";
}
