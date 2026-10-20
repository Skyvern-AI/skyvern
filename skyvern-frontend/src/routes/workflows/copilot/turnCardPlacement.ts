import {
  ActivityEntry,
  TurnNarrativeState,
  toolCallIdOf,
} from "./narrativeState";

// A call made while a block runs is filed under that block, not the turn's own activity.
function turnActivity(turn: TurnNarrativeState): ActivityEntry[] {
  return [
    ...turn.designActivity,
    ...turn.blocks.flatMap((block) => block.activity),
  ];
}

// Mirror of the backend's credential tool names (tools/__init__.py).
const CREDENTIAL_TOOLS = new Set([
  "request_credential",
  "fill_credential_field",
]);

// Where a credential receipt renders: the call recorded with it, else the turn's first credential
// tool call, else nowhere in the log (null), which places it above the turn's reply.
export function credentialAnchorToolCallId(
  turn: TurnNarrativeState,
  recorded?: string | null,
): string | null {
  if (recorded) return recorded;
  const entry = turnActivity(turn).find(
    (candidate) =>
      candidate.toolName !== undefined &&
      CREDENTIAL_TOOLS.has(candidate.toolName),
  );
  return (entry && toolCallIdOf(entry)) ?? null;
}
