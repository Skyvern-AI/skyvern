import type { ChatMessage } from "./WorkflowCopilotChat";

// Rows the FE synthesises for display. They are never persisted and never sent to the LLM, so
// none of them is an answer — `ChatMessage.kind` is the closed set, and skipping only one of them
// leaves the other reading as one.
const SYNTHETIC_KINDS: ReadonlySet<string> = new Set([
  "run_lifecycle",
  "status_notice",
]);

// The next message a user could have answered with. Raw messages[index + 1] would let a synthetic
// row appended after an ask read as an answer: the card would go read-only, discarding anything
// half-typed in its fields, and a real answer arriving later would no longer be the receipt
// source. Its own module so the chat file keeps exporting only components (Fast Refresh).
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

// The two adjacency-derived props the question card renders from. Exported as one function so the
// render and the test share a single source: asserting nextAnsweringMessage alone left both props
// free to revert to raw adjacency while the tests stayed green.
export function questionCardAdjacency(
  messages: ChatMessage[],
  index: number,
): { answeredFrom: string | null; hasFollowingMessage: boolean } {
  const answering = nextAnsweringMessage(messages, index);
  return {
    answeredFrom: answering?.sender === "user" ? answering.content : null,
    hasFollowingMessage: answering !== undefined,
  };
}
