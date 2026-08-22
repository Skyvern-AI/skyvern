// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  EMPTY_NARRATIVE,
  hydrateNarrativeFromPayload,
  type TurnNarrativeState,
} from "../narrativeState";
import { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";
import { derivePhases } from "../copilotPhases";
import { ReviewGateCard, getReviewGateVerdict } from "./ReviewGateCard";

const failedBlock = {
  workflowRunBlockId: "wrb_failed",
  label: "add_to_cart",
  blockType: "task",
  state: "failed" as const,
  lastSeenIteration: 0,
  activity: [],
  startedAt: null,
  endedAt: null,
};

const completedBlock = {
  ...failedBlock,
  workflowRunBlockId: "wrb_done",
  label: "open_page",
  state: "completed" as const,
};

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

  it("has no verdict for a legacy proposal carrying no evidence either way", () => {
    const legacyProposal = {} as unknown as WorkflowApiResponse;
    expect(
      getReviewGateVerdict(turn({ proposalDisposition: null }), legacyProposal),
    ).toBe(null);
  });

  it("returns null with no disposition and no proposal", () => {
    expect(
      getReviewGateVerdict(turn({ proposalDisposition: null }), null),
    ).toBe(null);
  });

  it("returns null for a turn-less call, so a pending gate cannot invent a verdict", () => {
    const proposal = {} as unknown as WorkflowApiResponse;
    expect(getReviewGateVerdict(undefined, proposal)).toBe(null);
  });

  it("never reports tested while the turn's own rail computes a failed test phase", () => {
    const failedTurn = turn({
      proposalDisposition: "review_tested",
      blocks: [failedBlock],
    });

    expect(
      derivePhases(failedTurn).find((row) => row.id === "test")?.status,
    ).toBe("fail");
    expect(getReviewGateVerdict(failedTurn, null)).not.toBe("tested");
  });
});

describe("ReviewGateCard — untested proposals stay actionable", () => {
  const noop = () => {};

  it("keeps Accept and Always accept enabled with an untested verdict", () => {
    render(
      <ReviewGateCard
        turn={turn({ proposalDisposition: "review_untested" })}
        pending
        verdict="untested"
        actionsEnabled
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
      />,
    );

    const accept = screen.getByRole("button", { name: /^Accept$/ });
    const alwaysAccept = screen.getByRole("button", { name: /Always accept/ });
    expect(accept.hasAttribute("disabled")).toBe(false);
    expect(alwaysAccept.hasAttribute("disabled")).toBe(false);
  });
});

describe("ReviewGateCard — Test end-to-end recourse", () => {
  const noop = () => {};

  it("keeps Accept working on an untested proposal that never ran end-to-end", () => {
    let accepted = 0;
    render(
      <ReviewGateCard
        turn={turn({ proposalDisposition: "review_untested" })}
        pending
        verdict="untested"
        actionsEnabled
        onAccept={() => {
          accepted += 1;
        }}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
        onTestEndToEnd={noop}
      />,
    );

    const accept = screen.getByRole("button", { name: /^Accept$/ });
    expect(accept.hasAttribute("disabled")).toBe(false);
    fireEvent.click(accept);
    expect(accepted).toBe(1);
  });

  it("does not claim every step was tested when one of them never ran", () => {
    render(
      <ReviewGateCard
        turn={turn({
          proposalDisposition: "review_untested",
          blocks: [
            completedBlock,
            { ...failedBlock, state: "drafted" as const },
          ],
        })}
        pending
        verdict="untested"
        actionsEnabled
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
        onTestEndToEnd={noop}
      />,
    );

    expect(screen.queryByText(/Each step was tested on its own/)).toBeNull();
  });

  it("says only that the run acts for real when no step has been tested yet", () => {
    render(
      <ReviewGateCard
        turn={turn({ proposalDisposition: "review_untested", blocks: [] })}
        pending
        verdict="untested"
        actionsEnabled
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
        onTestEndToEnd={noop}
      />,
    );

    expect(
      screen.getByRole("button", { name: /Test end-to-end/ }),
    ).not.toBeNull();
    expect(screen.queryByText(/Each step was tested on its own/)).toBeNull();
    expect(
      screen.getByText(/performs real actions on the site/).textContent,
    ).toContain("place orders");
  });

  it("states that steps were tested alone, not together, and that the run acts on the site", () => {
    render(
      <ReviewGateCard
        turn={turn({
          proposalDisposition: "review_untested",
          blocks: [completedBlock],
        })}
        pending
        verdict="untested"
        actionsEnabled
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
        onTestEndToEnd={noop}
      />,
    );

    expect(
      screen.getByRole("button", { name: /Test end-to-end/ }),
    ).not.toBeNull();
    const explainer = screen.getByText(/Each step was tested on its own/);
    expect(explainer.textContent).toContain("have not been run together");
    expect(explainer.textContent).toContain(
      "performs real actions on the site",
    );
  });
});

describe("ReviewGateCard — block label humanization", () => {
  const noop = () => {};

  it("renders legacy proposals neutrally while keeping the raw label in a title attribute", () => {
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

    expect(screen.getByText("Proposed blocks")).not.toBeNull();
    const proposed = screen.getByText("Extract Titles");
    expect(proposed.getAttribute("title")).toBe("extract_titles_v2");
    expect(screen.queryByText("Added")).toBeNull();
    expect(screen.queryByText("Removed")).toBeNull();
    expect(screen.queryByText("Old Extract Step")).toBeNull();
  });
});

describe("ReviewGateCard — recorded review projection", () => {
  const noop = () => {};

  it("renders all change classes, never-tested markers, and duplicate notes without disabling Accept", () => {
    render(
      <ReviewGateCard
        turn={turn({
          review: {
            blocks: [
              {
                label: "added_export",
                blockType: "google_sheets_write",
                change: "added",
                neverTested: true,
              },
              {
                label: "changed_query",
                blockType: "task",
                change: "changed",
                neverTested: false,
              },
              {
                label: "unchanged_login",
                blockType: "login",
                change: "unchanged",
                neverTested: true,
              },
              {
                label: "removed_cleanup",
                blockType: "task",
                change: "removed",
              },
            ],
            duplicateWrites: [
              {
                blockType: "google_sheets_write",
                blockLabels: ["added_export", "backup_export"],
              },
            ],
          },
        })}
        pending
        verdict="untested"
        actionsEnabled
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
      />,
    );

    expect(screen.getByText("Added")).not.toBeNull();
    expect(screen.getByText("+ Added Export")).not.toBeNull();
    expect(screen.getByText("Changed")).not.toBeNull();
    expect(screen.getByText("~ Changed Query")).not.toBeNull();
    expect(screen.getByText("Unchanged")).not.toBeNull();
    expect(screen.getByText("Unchanged Login")).not.toBeNull();
    expect(screen.getByText("Removed")).not.toBeNull();
    expect(screen.getByText("- Removed Cleanup")).not.toBeNull();
    expect(screen.getAllByText("Never tested")).toHaveLength(2);
    expect(
      screen.getByText(
        "Added Export and Backup Export write to the same destination.",
      ),
    ).not.toBeNull();
    expect(
      screen.getByRole("button", { name: "Accept" }).hasAttribute("disabled"),
    ).toBe(false);
    expect(
      screen
        .getByRole("button", { name: "Always accept" })
        .hasAttribute("disabled"),
    ).toBe(false);
  });

  it("hydrates the optional projection and ignores malformed review payloads", () => {
    const basePayload = {
      turnId: "turn-1",
      turnIndex: 0,
      terminal: "response",
      blocks: [],
    };
    const hydrated = hydrateNarrativeFromPayload({
      ...basePayload,
      review: {
        blocks: [
          {
            label: "saved_step",
            blockType: "task",
            change: "unchanged",
            neverTested: false,
          },
        ],
        duplicateWrites: [],
      },
    });
    const malformed = hydrateNarrativeFromPayload({
      ...basePayload,
      review: { blocks: "not-an-array", duplicateWrites: [] },
    });

    expect(hydrated?.review?.blocks[0]?.label).toBe("saved_step");
    expect(malformed?.review).toBeNull();
  });
});
