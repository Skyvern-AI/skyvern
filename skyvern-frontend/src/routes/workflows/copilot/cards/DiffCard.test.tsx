// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { EMPTY_NARRATIVE, type TurnNarrativeState } from "../narrativeState";
import { DiffCard } from "./DiffCard";
import { getDiffCardTitle } from "./diffCardTitle";

afterEach(() => {
  cleanup();
});

const turn = (
  overrides: Partial<TurnNarrativeState> = {},
): TurnNarrativeState => ({
  ...EMPTY_NARRATIVE,
  turnId: "turn-1",
  turnIndex: 0,
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

  it("labels a manually accepted review_tested draft as applied changes", () => {
    expect(
      getDiffCardTitle(turn({ proposalDisposition: "review_tested" }), {
        accepted: true,
      }),
    ).toBe("Applied changes");
  });

  it("still lets a backend-supplied summary win over accepted", () => {
    expect(
      getDiffCardTitle(
        turn({
          proposalDisposition: "review_tested",
          draft: {
            blockCount: 1,
            blockLabels: ["block_1"],
            summary: "Added browser step",
          },
        }),
        { accepted: true },
      ),
    ).toBe("Added browser step");
  });
});

describe("DiffCard", () => {
  it("lists a removed block from the review projection, which carries the removals", () => {
    render(
      <DiffCard
        turn={turn({
          review: {
            blocks: [
              { label: "block_1", blockType: "task", change: "added" },
              { label: "old_cleanup", blockType: "task", change: "removed" },
            ],
            duplicateWrites: [],
          },
        })}
      />,
    );

    expect(screen.getByText("Removed")).not.toBeNull();
    expect(screen.getByText("- old_cleanup")).not.toBeNull();
  });

  it("shows no removed section when the projection reports no removals", () => {
    render(<DiffCard turn={turn()} />);

    expect(screen.queryByText("Removed")).toBeNull();
  });
});
