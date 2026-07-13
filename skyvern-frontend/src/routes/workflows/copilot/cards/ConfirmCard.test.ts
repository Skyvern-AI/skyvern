import { describe, expect, it } from "vitest";

import { EMPTY_NARRATIVE, type TurnNarrativeState } from "../narrativeState";
import { shouldShowConfirmCard } from "./ConfirmCard";

const turn = (
  overrides: Partial<TurnNarrativeState> = {},
): TurnNarrativeState => ({
  ...EMPTY_NARRATIVE,
  turnId: "turn-1",
  turnIndex: 0,
  mode: "diagnose",
  terminal: "response",
  ...overrides,
});

describe("shouldShowConfirmCard", () => {
  it("matches the real backend confirm-request note (agent.py _with_inline_reject_note)", () => {
    expect(
      shouldShowConfirmCard(
        turn({
          terminalMessage:
            "(Note: Diagnosing a failed run doesn't edit the workflow on its own — confirm and I'll apply the change.)",
        }),
      ),
    ).toBe(true);
  });

  it("is case-insensitive and tolerant of curly apostrophes", () => {
    expect(
      shouldShowConfirmCard(
        turn({ terminalMessage: "CONFIRM AND I’LL APPLY it now." }),
      ),
    ).toBe(true);
  });

  it("checks narrativeSummary too", () => {
    expect(
      shouldShowConfirmCard(
        turn({ narrativeSummary: "please confirm and I'll apply the fix" }),
      ),
    ).toBe(true);
  });

  it("does not fire on unrelated QA prose", () => {
    expect(
      shouldShowConfirmCard(
        turn({
          terminalMessage:
            "I found two saved credentials — which one should I use?",
        }),
      ),
    ).toBe(false);
  });

  it("does not fire while the turn is still in flight", () => {
    expect(
      shouldShowConfirmCard(
        turn({
          terminal: null,
          terminalMessage: "confirm and I'll apply the change",
        }),
      ),
    ).toBe(false);
  });

  it("does not fire on an error terminal even if the text happens to match", () => {
    expect(
      shouldShowConfirmCard(
        turn({
          terminal: "error",
          terminalMessage: "confirm and I'll apply the change",
        }),
      ),
    ).toBe(false);
  });
});
