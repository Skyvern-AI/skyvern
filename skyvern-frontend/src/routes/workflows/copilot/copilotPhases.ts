import {
  AUTHORING_TOOLS,
  RUN_TOOLS,
  TurnNarrativeState,
  latestBlocksByLabel,
} from "./narrativeState";

export { AUTHORING_TOOLS, RUN_TOOLS };

// Byte-for-byte the existing showDesign gate (NarrativeView.tsx) so Q&A /
// clarify turns keep today's behavior: checklist appears live, disappears at
// a no-build terminal.
export function showPhaseChecklist(turn: TurnNarrativeState): boolean {
  return (
    turn.designStarted &&
    ((turn.draft?.blockCount ?? 0) > 0 ||
      turn.blocks.length > 0 ||
      turn.terminal === null)
  );
}

export function hasFailedTestBlock(turn: TurnNarrativeState): boolean {
  return latestBlocksByLabel(turn.blocks)
    .filter((b) => b.state !== "drafted")
    .some((b) => b.state === "failed");
}

export function everyTestBlockExecuted(turn: TurnNarrativeState): boolean {
  // Gates a claim about every step, so only the proposal's own tested state can answer it. The
  // projection is also absent when the proposal would not parse, and an unverifiable turn claims nothing.
  const review = turn.review;
  if (review === null) return false;
  const proposed = review.blocks.filter((b) => b.change !== "removed");
  return proposed.length > 0 && proposed.every((b) => b.neverTested !== true);
}
