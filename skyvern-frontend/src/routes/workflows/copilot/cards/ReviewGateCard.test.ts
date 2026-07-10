import { describe, expect, it } from "vitest";

import { EMPTY_NARRATIVE, type TurnNarrativeState } from "../narrativeState";
import { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";
import { getReviewGateVerdict } from "./ReviewGateCard";

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
