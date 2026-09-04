// The composer's placeholder, as a pure decision so the ask branch is testable without mounting
// the whole chat. Order is precedence: transient states outrank the standing invitation.
export function composerPlaceholder({
  queuedPrompt,
  isLoading,
  isWaitingForLiveBrowser,
  latestTurnIsAsk,
  copilotV2Enabled,
}: {
  queuedPrompt: boolean;
  isLoading: boolean;
  isWaitingForLiveBrowser: boolean;
  latestTurnIsAsk: boolean;
  copilotV2Enabled: boolean;
}): string {
  if (queuedPrompt) return "Type to replace the queued message…";
  if (isLoading) return "Type to queue a message…";
  if (isWaitingForLiveBrowser) return "Type a prompt to send when ready...";
  // While a question is pending the composer is the answer path for anything the card cannot
  // take, so it says so rather than inviting an unrelated new request.
  if (latestTurnIsAsk) return "Answer Copilot…";
  return copilotV2Enabled
    ? "Ask Copilot to build or change your workflow…"
    : "Message Skyvern Copilot, or paste recorded steps…";
}
