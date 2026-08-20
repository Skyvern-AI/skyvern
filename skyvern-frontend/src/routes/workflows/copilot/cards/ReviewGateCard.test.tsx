// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  EMPTY_NARRATIVE,
  hydrateNarrativeFromPayload,
  type TurnNarrativeState,
} from "../narrativeState";
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
