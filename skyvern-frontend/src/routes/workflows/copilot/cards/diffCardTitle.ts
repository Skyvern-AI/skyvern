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
  if (pendingProposal || !draftLanded(turn, { rejected })) {
    return "Proposed changes";
  }
  return "Applied changes";
}

// "Applied" requires the backend's explicit auto-applied signal. A rejected, cancelled or errored
// draft, or a null/unknown disposition from a forward-compatible backend, never landed.
export function draftLanded(
  turn: TurnNarrativeState,
  {
    rejected = false,
    accepted = false,
  }: Pick<DiffCardTitleOptions, "rejected" | "accepted"> = {},
): boolean {
  if (accepted) {
    return true;
  }
  return (
    !rejected &&
    !turn.cancelled &&
    turn.terminal !== "error" &&
    turn.proposalDisposition === "auto_applicable"
  );
}
