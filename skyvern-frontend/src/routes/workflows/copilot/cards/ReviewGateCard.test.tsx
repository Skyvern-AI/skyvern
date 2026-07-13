// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { EMPTY_NARRATIVE, type TurnNarrativeState } from "../narrativeState";
import { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";
import { ReviewGateCard, getReviewGateVerdict } from "./ReviewGateCard";

afterEach(() => {
  cleanup();
});

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
  proposalDisposition: "review_untested",
  terminal: "response",
  ...overrides,
});

describe("getReviewGateVerdict", () => {
  it("treats review_tested as tested", () => {
    expect(
      getReviewGateVerdict(
        turn({ proposalDisposition: "review_tested" }),
        null,
      ),
    ).toBe("tested");
  });

  it("treats auto_applicable as tested — the backend only assigns this disposition to verified changes", () => {
    expect(
      getReviewGateVerdict(
        turn({ proposalDisposition: "auto_applicable" }),
        null,
      ),
    ).toBe("tested");
  });

  it("treats review_untested as untested", () => {
    expect(
      getReviewGateVerdict(
        turn({ proposalDisposition: "review_untested" }),
        null,
      ),
    ).toBe("untested");
  });

  it("falls back to the legacy _copilot_unvalidated marker when the turn has no disposition", () => {
    const legacyProposal = {
      _copilot_unvalidated: true,
    } as unknown as WorkflowApiResponse;
    expect(
      getReviewGateVerdict(turn({ proposalDisposition: null }), legacyProposal),
    ).toBe("untested");
  });

  it("falls back to tested for a legacy proposal without the unvalidated marker", () => {
    const legacyProposal = {} as unknown as WorkflowApiResponse;
    expect(
      getReviewGateVerdict(turn({ proposalDisposition: null }), legacyProposal),
    ).toBe("tested");
  });

  it("returns null with no disposition and no proposal", () => {
    expect(
      getReviewGateVerdict(turn({ proposalDisposition: null }), null),
    ).toBe(null);
  });
});

describe("ReviewGateCard — block label humanization", () => {
  const noop = () => {};

  it("humanizes Added/Removed block labels, keeping the raw label in a title attribute", () => {
    render(
      <ReviewGateCard
        turn={turn({
          draft: {
            blockCount: 1,
            blockLabels: ["extract_titles_v2"],
            summary: null,
          },
          blocks: [
            {
              workflowRunBlockId: "wrb_1",
              label: "old_extract_step",
              blockType: "task",
              state: "drafted",
              lastSeenIteration: 0,
              activity: [],
              startedAt: null,
              endedAt: null,
            },
          ],
        })}
        pending={false}
        verdict={null}
        actionsEnabled={false}
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
      />,
    );

    const added = screen.getByText("+ Extract Titles");
    expect(added.getAttribute("title")).toBe("extract_titles_v2");
    expect(screen.queryByText("+ extract_titles_v2")).toBeNull();

    const removed = screen.getByText("- Old Extract Step");
    expect(removed.getAttribute("title")).toBe("old_extract_step");
  });
});
