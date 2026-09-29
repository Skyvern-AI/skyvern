// The composer's placeholder, as a pure decision so the ask branch is testable without mounting
// the whole chat. Order is precedence: transient states outrank the standing invitation.
export function composerPlaceholder({
  queuedPrompt,
  isLoading,
  isWaitingForLiveBrowser,
  latestTurnIsAsk,
  askPartHasChoices = false,
}: {
  // Whether a send adds to the queued message or replaces it (a programmatic one).
  queuedPrompt: "add" | "replace" | null;
  isLoading: boolean;
  isWaitingForLiveBrowser: boolean;
  latestTurnIsAsk: boolean;
  // Whether the question on screen offers choices; without them the composer is the answer.
  askPartHasChoices?: boolean;
}): string {
  if (queuedPrompt === "add") return "Add to the queued message…";
  if (queuedPrompt === "replace") return "Type to replace the queued message…";
  if (isLoading) return "Type to queue a message…";
  if (isWaitingForLiveBrowser) return "Type a prompt to send when ready...";
  // While a question is pending the composer is the text field for the question on screen, so
  // it says so rather than inviting an unrelated new request.
  if (latestTurnIsAsk)
    return askPartHasChoices ? "Add details (optional)…" : "Type your answer…";
  return "Ask Copilot to build or change your workflow…";
}
