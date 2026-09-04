import { describe, expect, it } from "vitest";

import { nextAnsweringMessage, questionCardAdjacency } from "./cardAdjacency";
import type { ChatMessage } from "./WorkflowCopilotChat";

const msg = (
  id: string,
  sender: ChatMessage["sender"],
  overrides: Partial<ChatMessage> = {},
): ChatMessage =>
  ({
    id,
    sender,
    content: id,
    timestamp: "2026-09-04T00:00:00Z",
    ...overrides,
  }) as ChatMessage;

const lifecycle = (id: string) =>
  msg(id, "ai", { kind: "run_lifecycle" } as Partial<ChatMessage>);

describe("nextAnsweringMessage", () => {
  // The question card treats the following message as the answer: it goes read-only and reads its
  // receipt from it. A run_lifecycle row is synthetic, so counting it would hide the fields
  // mid-typing and point the receipt at a line the user never wrote.
  it("skips a lifecycle row appended after an ask and finds the real answer", () => {
    const messages = [
      msg("ask", "ai"),
      lifecycle("run-started"),
      msg("answer", "user"),
    ];

    expect(nextAnsweringMessage(messages, 0)?.id).toBe("answer");
  });

  it("reports nothing answerable when only lifecycle rows follow the ask", () => {
    const messages = [msg("ask", "ai"), lifecycle("run-started")];

    expect(nextAnsweringMessage(messages, 0)).toBeUndefined();
  });

  it("still finds an immediately adjacent answer", () => {
    const messages = [msg("ask", "ai"), msg("answer", "user")];

    expect(nextAnsweringMessage(messages, 0)?.id).toBe("answer");
  });
});

// The card renders BOTH of its adjacency props from questionCardAdjacency. Asserting
// nextAnsweringMessage alone left either prop free to revert to raw messages[index + 1] with the
// suite still green — the green-under-mutation shape SKY-15619 was filed for.
describe("questionCardAdjacency", () => {
  it("derives both card props across a synthetic row", () => {
    const messages = [
      msg("ask", "ai"),
      lifecycle("run-started"),
      msg("answer", "user"),
    ];

    expect(questionCardAdjacency(messages, 0)).toEqual({
      answeredFrom: "answer",
      hasFollowingMessage: true,
    });
  });

  it("leaves the card live when only a synthetic row follows the ask", () => {
    const messages = [msg("ask", "ai"), lifecycle("run-started")];

    expect(questionCardAdjacency(messages, 0)).toEqual({
      answeredFrom: null,
      hasFollowingMessage: false,
    });
  });

  it("reports a following AI turn as consuming without offering it as a receipt", () => {
    const messages = [msg("ask", "ai"), msg("next-turn", "ai")];

    expect(questionCardAdjacency(messages, 0)).toEqual({
      answeredFrom: null,
      hasFollowingMessage: true,
    });
  });
});
