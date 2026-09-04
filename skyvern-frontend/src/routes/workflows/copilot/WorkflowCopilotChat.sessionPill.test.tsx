// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ChatMessage, ConvoAggregatePill } from "./WorkflowCopilotChat";
import { EMPTY_NARRATIVE, TurnNarrativeState } from "./narrativeState";

afterEach(cleanup);

const askingEnvelope = (renderedFromEnvelope: boolean) => ({
  nextState: "awaiting_user_input" as const,
  renderedFromEnvelope,
  runVerdict: null,
  runDisplayReason: null,
});

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

const pill = (messages: ChatMessage[]) => {
  render(<ConvoAggregatePill messages={messages} isInFlight={false} />);
  return screen.getByText(/turns/).textContent ?? "";
};

describe("ConvoAggregatePill — session status", () => {
  it("says the session is waiting on the user, outranking an earlier halt", () => {
    expect(
      pill([
        turn("t1", { terminal: "error" }),
        turn("t2", { terminalEnvelope: askingEnvelope(true) }),
      ]),
    ).toContain("Waiting on you");
  });

  it("does not stay blocked on a question a later turn already moved past", () => {
    expect(
      pill([
        turn("t1", { terminalEnvelope: askingEnvelope(true) }),
        turn("t2"),
      ]),
    ).toContain("Done");
  });

  it("stops waiting once the user has replied", () => {
    expect(
      pill([
        turn("t1"),
        turn("t2", { terminalEnvelope: askingEnvelope(true) }),
        { id: "u1", sender: "user", content: "the member portal" },
      ]),
    ).not.toContain("Waiting on you");
  });

  it("stops waiting on a later turn that carried no narrative", () => {
    expect(
      pill([
        turn("t1"),
        turn("t2", { terminalEnvelope: askingEnvelope(true) }),
        { id: "a3", sender: "ai", content: "Answered in ask mode." },
      ]),
    ).not.toContain("Waiting on you");
  });

  it("keeps waiting behind a trailing run-lifecycle line", () => {
    expect(
      pill([
        turn("t1"),
        turn("t2", { terminalEnvelope: askingEnvelope(true) }),
        {
          id: "rl1",
          sender: "ai",
          content: "Run started - watching it now.",
          kind: "run_lifecycle",
        },
      ]),
    ).toContain("Waiting on you");
  });

  it("does not wait on a user who stopped the turn themselves", () => {
    expect(
      pill([
        turn("t1"),
        turn("t2", {
          terminalEnvelope: askingEnvelope(true),
          terminal: "error",
          cancelled: true,
        }),
      ]),
    ).not.toContain("Waiting on you");
  });

  it("keeps the legacy status until the backend stamps the envelope", () => {
    expect(
      pill([
        turn("t1"),
        turn("t2", { terminalEnvelope: askingEnvelope(false) }),
      ]),
    ).toContain("Done");
  });
});
