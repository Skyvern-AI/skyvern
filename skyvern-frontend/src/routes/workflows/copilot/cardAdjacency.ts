import type { ChatMessage } from "./WorkflowCopilotChat";

// Rows the FE synthesises for display. They are never persisted and never sent to the LLM, so
// none of them is an answer — `ChatMessage.kind` is the closed set, and skipping only one of them
// leaves the other reading as one.
const SYNTHETIC_KINDS: ReadonlySet<string> = new Set([
  "run_lifecycle",
  "status_notice",
]);

// Account-choice controls and receipts follow conversation messages, skipping display-only rows.
export function nextAnsweringMessage(
  messages: ChatMessage[],
  index: number,
): ChatMessage | undefined {
  for (let next = index + 1; next < messages.length; next += 1) {
    const candidate = messages[next];
    if (!SYNTHETIC_KINDS.has(candidate?.kind ?? "")) return candidate;
  }
  return undefined;
}

// The ask a user's message answered. Raw messages[index - 1] would let a synthetic row inserted
// between an ask and its answer hide the ask, so an account-selection bubble could no longer map
// its raw connection id to the friendly "Selected …" receipt and would surface the raw id instead.
export function previousAskingMessage(
  messages: ChatMessage[],
  index: number,
): ChatMessage | undefined {
  for (let prev = index - 1; prev >= 0; prev -= 1) {
    const candidate = messages[prev];
    if (!SYNTHETIC_KINDS.has(candidate?.kind ?? "")) return candidate;
  }
  return undefined;
}
