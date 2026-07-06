import { describe, expect, it } from "vitest";

import { EMPTY_NARRATIVE, type TurnNarrativeState } from "../narrativeState";
import { getDiffCardTitle } from "./diffCardTitle";

const turn = (
  overrides: Partial<TurnNarrativeState> = {},
): TurnNarrativeState => ({
  ...EMPTY_NARRATIVE,
  turnId: "turn-1",
  turnIndex: 0,
  mode: "build",
  designStarted: true,
  designEnded: true,
  draft: {
    blockCount: 1,
    blockLabels: ["block_1"],
    summary: null,
  },
  proposalDisposition: "auto_applicable",
  terminal: "response",
  ...overrides,
});

describe("getDiffCardTitle", () => {
  it("labels auto-applied workflow updates as applied changes", () => {
    expect(getDiffCardTitle(turn())).toBe("Applied changes");
  });

  it("labels pending proposals as proposed changes", () => {
    expect(getDiffCardTitle(turn(), { pendingProposal: true })).toBe(
      "Proposed changes",
    );
  });

  it.each(["review_untested", "review_tested"] as const)(
    "keeps %s drafts labeled as proposed changes",
    (proposalDisposition) => {
      expect(getDiffCardTitle(turn({ proposalDisposition }))).toBe(
        "Proposed changes",
      );
    },
  );

  it("never labels a rejected auto-applicable draft as applied changes", () => {
    expect(getDiffCardTitle(turn(), { rejected: true })).toBe(
      "Proposed changes",
    );
  });

  it("labels cancelled auto-applicable drafts as proposed changes", () => {
    expect(getDiffCardTitle(turn({ cancelled: true }))).toBe(
      "Proposed changes",
    );
  });

  it("labels errored auto-applicable drafts as proposed changes", () => {
    expect(getDiffCardTitle(turn({ terminal: "error" }))).toBe(
      "Proposed changes",
    );
  });

  it("defaults an unknown or missing disposition to proposed changes", () => {
    expect(getDiffCardTitle(turn({ proposalDisposition: null }))).toBe(
      "Proposed changes",
    );
  });

  it("preserves a backend-supplied draft summary", () => {
    expect(
      getDiffCardTitle(
        turn({
          draft: {
            blockCount: 1,
            blockLabels: ["block_1"],
            summary: "Added browser step",
          },
        }),
      ),
    ).toBe("Added browser step");
  });
});
