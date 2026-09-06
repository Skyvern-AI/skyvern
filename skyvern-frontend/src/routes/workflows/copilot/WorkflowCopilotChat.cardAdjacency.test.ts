import { describe, expect, it } from "vitest";

import { nextAnsweringMessage, previousAskingMessage } from "./cardAdjacency";
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

describe("previousAskingMessage", () => {
  // The account-selection receipt maps the user's raw connection id back to its friendly label
  // via the ask above it. A synthetic row sitting between the ask and the selection must be
  // skipped, or the receipt reads null and the transcript exposes the raw id.
  it("skips a lifecycle row inserted before the answer and finds the real ask", () => {
    const messages = [
      msg("ask", "ai"),
      lifecycle("run-started"),
      msg("answer", "user"),
    ];

    expect(previousAskingMessage(messages, 2)?.id).toBe("ask");
  });

  it("reports nothing prior when only synthetic rows precede the answer", () => {
    const messages = [lifecycle("run-started"), msg("answer", "user")];

    expect(previousAskingMessage(messages, 1)).toBeUndefined();
  });

  it("still finds an immediately adjacent ask", () => {
    const messages = [msg("ask", "ai"), msg("answer", "user")];

    expect(previousAskingMessage(messages, 1)?.id).toBe("ask");
  });
});
