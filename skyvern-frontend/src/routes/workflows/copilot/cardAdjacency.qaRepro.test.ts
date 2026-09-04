// Pins: no FE-synthetic row may read as the user's answer. `ChatMessage.kind` is a closed set of
// two — `run_lifecycle` and `status_notice` — and a guard naming only one lets the other through,
// which sends the question card read-only mid-typing and makes its receipt source a line no user
// wrote. Origin: QA repro, adopted into #16625.

import { describe, expect, it } from "vitest";

import { nextAnsweringMessage } from "./cardAdjacency";
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

const statusNotice = (id: string) =>
  msg(id, "ai", { kind: "status_notice" } as Partial<ChatMessage>);

describe("nextAnsweringMessage — QA repro", () => {
  it("skips a status_notice row the way it skips run_lifecycle", () => {
    const messages = [
      msg("ask", "ai"),
      statusNotice("stop"),
      msg("answer", "user"),
    ];

    expect(nextAnsweringMessage(messages, 0)?.id).toBe("answer");
  });

  it("reports nothing answerable when only a status_notice follows the ask", () => {
    const messages = [msg("ask", "ai"), statusNotice("stop")];

    expect(nextAnsweringMessage(messages, 0)).toBeUndefined();
  });
});
