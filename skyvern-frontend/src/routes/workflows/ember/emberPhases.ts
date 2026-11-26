import {
  AUTHORING_TOOLS,
  BlockState,
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

// The backend's refusal code for a Copilot test run denied an account (EMBER_TEST_RUN_UNSUPPORTED_CODE).
const ACCESS_REFUSED_FAILURE_CODES = new Set(["copilot_test_run_unsupported"]);

// True when the test failed only because it was refused access, so it says nothing about the draft. A loop row
// carries no code of its own, so it counts as refused when every failed block under it was refused.
export function failedOnlyOnRefusedAccess(turn: TurnNarrativeState): boolean {
  const refused = (b: BlockState): boolean => {
    if (b.failureCode !== undefined) {
      return ACCESS_REFUSED_FAILURE_CODES.has(b.failureCode);
    }
    const failedChildren = turn.blocks.filter(
      (child) =>
        child.state === "failed" &&
        child.parentWorkflowRunBlockId !== undefined &&
        child.parentWorkflowRunBlockId === b.workflowRunBlockId,
    );
    return failedChildren.length > 0 && failedChildren.every(refused);
  };
  const failed = latestBlocksByLabel(turn.blocks).filter(
    (b) => b.state === "failed",
  );
  return failed.length > 0 && failed.every(refused);
}

export function everyTestBlockExecuted(turn: TurnNarrativeState): boolean {
  // Gates a claim about every step, so only the proposal's own tested state can answer it. The
  // projection is also absent when the proposal would not parse, and an unverifiable turn claims nothing.
  const review = turn.review;
  if (review === null) return false;
  const proposed = review.blocks.filter((b) => b.change !== "removed");
  return proposed.length > 0 && proposed.every((b) => b.neverTested !== true);
}
