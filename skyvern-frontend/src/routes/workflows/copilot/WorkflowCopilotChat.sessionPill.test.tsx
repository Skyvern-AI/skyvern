// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ChatMessage, ConvoAggregatePill } from "./WorkflowCopilotChat";
import { EMPTY_NARRATIVE, TurnNarrativeState } from "./narrativeState";

afterEach(cleanup);

const turn = (
  id: string,
  overrides: Partial<TurnNarrativeState> = {},
): ChatMessage => ({
  id,
  sender: "ai",
  content: "",
  timestamp: "2026-09-03T00:00:05Z",
  narrative: {
    ...EMPTY_NARRATIVE,
    turnId: id,
    terminal: "response",
    startedAt: "2026-09-03T00:00:00Z",
    endedAt: "2026-09-03T00:00:05Z",
    ...overrides,
  },
});

const pill = (messages: ChatMessage[], hasPendingQuestion = false) => {
  render(
    <ConvoAggregatePill
      messages={messages}
      isInFlight={false}
      hasPendingQuestion={hasPendingQuestion}
    />,
  );
  return screen.getByText(/turns/).textContent ?? "";
};

describe("ConvoAggregatePill — session status", () => {
  it("says the session is waiting on the user, outranking an earlier halt", () => {
    expect(
      pill([turn("t1", { terminal: "error" }), turn("t2")], true),
    ).toContain("Waiting on you");
  });
});
