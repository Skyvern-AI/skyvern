// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  EMPTY_NARRATIVE,
  hydrateNarrativeFromPayload,
  type TurnNarrativeState,
} from "../narrativeState";
import { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";
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

// Radix menus open on pointerdown, not click.
function openMenu(name: string) {
  fireEvent.pointerDown(screen.getByRole("button", { name }), {
    button: 0,
    ctrlKey: false,
  });
}

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

const coveredFacts = {
  factsAvailable: true,
  authoredBlockCount: 1,
  matchingSourceBlockCount: 1,
  evaluationState: null,
  runId: "wr_1",
  runCompleted: true,
  terminalCause: null,
  blocksRunThisTurn: 1,
  ranCleanOnCurrentSource: true,
} as const;

describe("getReviewGateVerdict", () => {
  it("treats review_tested as tested when the turn's coverage facts back it", () => {
    expect(
      getReviewGateVerdict(
        turn({ proposalDisposition: "review_tested", turnFacts: coveredFacts }),
        null,
      ),
    ).toBe("tested");
  });

  it("treats auto_applicable as tested when the turn's coverage facts back it", () => {
    expect(
      getReviewGateVerdict(
        turn({
          proposalDisposition: "auto_applicable",
          turnFacts: coveredFacts,
        }),
        null,
      ),
    ).toBe("tested");
  });

  it.each(["review_tested", "auto_applicable"] as const)(
    "refuses a tested pill for %s when coverage is partial",
    (proposalDisposition) => {
      expect(
        getReviewGateVerdict(
          turn({
            proposalDisposition,
            // Partial coverage is decided by the backend, which publishes both the
            // count and the verdict it implies.
            turnFacts: {
              ...coveredFacts,
              matchingSourceBlockCount: 0,
              ranCleanOnCurrentSource: false,
            },
          }),
          null,
        ),
      ).toBe("untested");
    },
  );

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

  it("refuses a tested pill for a legacy proposal that carries no coverage facts", () => {
    const legacyProposal = {} as unknown as WorkflowApiResponse;
    expect(
      getReviewGateVerdict(turn({ proposalDisposition: null }), legacyProposal),
    ).toBe("untested");
    expect(
      getReviewGateVerdict(
        turn({ proposalDisposition: null, turnFacts: coveredFacts }),
        legacyProposal,
      ),
    ).toBe("tested");
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

  it("never reports tested for a turn with a failed test block", () => {
    const failedTurn = turn({
      proposalDisposition: "review_tested",
      turnFacts: coveredFacts,
      blocks: [failedBlock],
    });

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
        hasProposal
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
      />,
    );

    const accept = screen.getByRole("button", { name: /^Accept$/ });
    expect(accept.hasAttribute("disabled")).toBe(false);
    openMenu("More accept options");
    const alwaysAccept = screen.getByRole("menuitem", {
      name: /Always accept/,
    });
    expect(alwaysAccept.hasAttribute("data-disabled")).toBe(false);
  });
});

describe("ReviewGateCard — Test end-to-end recourse", () => {
  const noop = () => {};

  it("labels the typed connection-failure action as a fresh-session retry", () => {
    render(
      <ReviewGateCard
        turn={turn({
          proposalDisposition: "review_untested",
          turnFacts: {
            factsAvailable: true,
            evaluationState: null,
            runId: null,
            runCompleted: null,
            terminalCause: "already_closed",
            blocksRunThisTurn: null,
            ranCleanOnCurrentSource: false,
            authoredBlockCount: 0,
            matchingSourceBlockCount: 0,
          },
        })}
        pending
        verdict="untested"
        actionsEnabled
        hasProposal
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
        onTestEndToEnd={noop}
      />,
    );

    expect(
      screen.getByRole("button", { name: "Retry in a fresh session" }),
    ).not.toBeNull();
  });

  it("offers Billing instead of a fresh-session retry after credit admission refusal", () => {
    let testRuns = 0;
    render(
      <ReviewGateCard
        turn={turn({
          proposalDisposition: "review_untested",
          turnFacts: {
            factsAvailable: true,
            evaluationState: null,
            runId: null,
            runCompleted: null,
            terminalCause: "billing_credit_admission_refusal",
            blocksRunThisTurn: 0,
            ranCleanOnCurrentSource: false,
            authoredBlockCount: 1,
            matchingSourceBlockCount: 0,
          },
        })}
        pending
        verdict="untested"
        actionsEnabled
        hasProposal
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
        onTestEndToEnd={() => {
          testRuns += 1;
        }}
      />,
    );

    expect(
      screen.getByText(
        "No browser or run started because credits are exhausted.",
        { exact: false },
      ),
    ).not.toBeNull();
    expect(screen.queryByText(/browser credits/i)).toBeNull();
    const billing = screen.getByRole("link", { name: "Go to Billing" });
    expect(billing.getAttribute("href")).toBe("/billing");
    expect(screen.queryByRole("button", { name: /fresh session/i })).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Test end-to-end/i }),
    ).toBeNull();
    // Test end-to-end otherwise lives in this menu, so its absence is what proves no run is offered.
    expect(screen.queryByRole("button", { name: "More actions" })).toBeNull();
    expect(testRuns).toBe(0);
  });

  it("keeps Accept working on an untested proposal that never ran end-to-end", () => {
    let accepted = 0;
    render(
      <ReviewGateCard
        turn={turn({ proposalDisposition: "review_untested" })}
        pending
        verdict="untested"
        actionsEnabled
        hasProposal
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
        hasProposal
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
        onTestEndToEnd={noop}
      />,
    );

    expect(screen.queryByText(/Each step was tested on its own/)).toBeNull();
  });

  it("warns that the run acts for real and starts it only once confirmed", () => {
    let testRuns = 0;
    render(
      <ReviewGateCard
        turn={turn({ proposalDisposition: "review_untested", blocks: [] })}
        pending
        verdict="untested"
        actionsEnabled
        hasProposal
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
        onTestEndToEnd={() => {
          testRuns += 1;
        }}
      />,
    );

    expect(screen.queryByText(/Each step was tested on its own/)).toBeNull();
    openMenu("More actions");
    fireEvent.click(screen.getByRole("menuitem", { name: /Test end-to-end/ }));
    expect(
      screen.getByText(/performs real actions on the site/).textContent,
    ).toContain("place orders");
    expect(testRuns).toBe(0);
    // The confirmation replaces the row that held focus, so it takes focus itself.
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Run test" }),
    );

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText(/performs real actions on the site/)).toBeNull();
    expect(testRuns).toBe(0);
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "More actions" }),
    );

    openMenu("More actions");
    fireEvent.click(screen.getByRole("menuitem", { name: /Test end-to-end/ }));
    fireEvent.click(screen.getByRole("button", { name: "Run test" }));
    expect(testRuns).toBe(1);
  });

  it("drops an open confirmation and locks More actions when the gate locks", () => {
    const gate = (failure: "reload" | null) => (
      <ReviewGateCard
        turn={turn({ proposalDisposition: "review_untested", blocks: [] })}
        pending
        verdict="untested"
        actionsEnabled
        hasProposal
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
        onTestEndToEnd={noop}
        failure={failure}
        onRetry={noop}
      />
    );
    const { rerender } = render(gate(null));
    openMenu("More actions");
    fireEvent.click(screen.getByRole("menuitem", { name: /Test end-to-end/ }));
    expect(screen.queryByRole("button", { name: "Run test" })).not.toBeNull();

    rerender(gate("reload"));
    expect(screen.queryByRole("button", { name: "Run test" })).toBeNull();
    const more = screen.getByRole("button", { name: "More actions" });
    expect(more.hasAttribute("disabled")).toBe(true);
    openMenu("More actions");
    expect(screen.queryByRole("menuitem")).toBeNull();
  });

  it("withholds the every-step claim when the proposal could not be projected", () => {
    render(
      <ReviewGateCard
        turn={turn({ blocks: [completedBlock], review: null })}
        pending
        verdict="untested"
        actionsEnabled
        hasProposal
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
        onTestEndToEnd={noop}
      />,
    );

    expect(screen.queryByText(/Each step was tested on its own/)).toBeNull();
    openMenu("More actions");
    expect(
      screen.getByRole("menuitem", { name: /Test end-to-end/ }),
    ).not.toBeNull();
  });

  it("withholds the every-step claim when the proposal still holds an untested step", () => {
    render(
      <ReviewGateCard
        turn={turn({
          blocks: [completedBlock],
          review: {
            blocks: [
              { label: "open_page", blockType: "task", change: "changed" },
              {
                label: "untouched_step",
                blockType: "task",
                change: "unchanged",
                neverTested: true,
              },
            ],
            duplicateWrites: [],
          },
        })}
        pending
        verdict="untested"
        actionsEnabled
        hasProposal
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
        onTestEndToEnd={noop}
      />,
    );

    expect(screen.queryByText(/Each step was tested on its own/)).toBeNull();
    openMenu("More actions");
    expect(
      screen.getByRole("menuitem", { name: /Test end-to-end/ }),
    ).not.toBeNull();
  });

  it("states that steps were tested alone, not together, and that the run acts on the site", () => {
    render(
      <ReviewGateCard
        turn={turn({
          proposalDisposition: "review_untested",
          blocks: [completedBlock],
          review: {
            blocks: [
              {
                label: "open_page",
                blockType: "task",
                change: "changed",
                neverTested: false,
              },
            ],
            duplicateWrites: [],
          },
        })}
        pending
        verdict="untested"
        actionsEnabled
        hasProposal
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
        onTestEndToEnd={noop}
      />,
    );

    const explainer = screen.getByText(/Each step was tested on its own/);
    expect(explainer.textContent).toContain("not together");
    // The claim offers the test in place, so the overflow menu does not repeat it.
    expect(screen.queryByRole("button", { name: "More actions" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Test end-to-end" }));
    expect(
      screen.getByText(/performs real actions on the site/),
    ).not.toBeNull();
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
        hasProposal
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
      />,
    );

    // Never applied and never decided, so it must not wear the applied check.
    expect(screen.queryByText("✓")).toBeNull();
    expect(screen.queryByText(/Accepted and saved/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { expanded: false }));
    const proposed = screen.getByTitle("extract_titles_v2");
    expect(proposed.textContent).toBe("Extract Titles");
    // A legacy draft has no change classes, so its row carries no marker or change label.
    expect(proposed.parentElement?.previousElementSibling?.textContent).toBe(
      "",
    );
    expect(proposed.parentElement?.textContent).toBe("Extract Titles");
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
        hasProposal
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
      />,
    );

    const markerOf = (label: string) =>
      screen.getByTitle(label).parentElement?.previousElementSibling
        ?.textContent;
    expect(markerOf("added_export")).toBe("+");
    expect(markerOf("changed_query")).toBe("~");
    expect(markerOf("unchanged_login")).toBe("");
    expect(markerOf("removed_cleanup")).toBe("\u2212");
    expect(screen.getByTitle("removed_cleanup").className).toContain(
      "line-through",
    );
    expect(screen.getAllByText("Never tested")).toHaveLength(2);
    expect(
      screen.getByText(
        "Added Export and Backup Export write to the same destination.",
      ),
    ).not.toBeNull();
    expect(
      screen.getByRole("button", { name: "Accept" }).hasAttribute("disabled"),
    ).toBe(false);
    openMenu("More accept options");
    expect(
      screen
        .getByRole("menuitem", { name: /Always accept/ })
        .hasAttribute("data-disabled"),
    ).toBe(false);
  });

  it("never folds a removed block behind Show more, so a deletion is not accepted unseen", () => {
    render(
      <ReviewGateCard
        turn={turn({
          review: {
            blocks: [
              ...["one", "two", "three", "four", "five"].map((name) => ({
                label: `add_${name}`,
                blockType: "task",
                change: "added" as const,
              })),
              { label: "drop_invite", blockType: "task", change: "removed" },
            ],
            duplicateWrites: [],
          },
        })}
        pending
        verdict="untested"
        actionsEnabled
        hasProposal
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
      />,
    );

    expect(screen.queryByTitle("drop_invite")).not.toBeNull();
    expect(screen.queryByTitle("add_five")).toBeNull();
    expect(screen.getByRole("button", { name: "Show 1 more" })).not.toBeNull();
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

describe("ReviewGateCard — answered proposals collapse", () => {
  const noop = () => {};

  it("shrinks an accepted proposal to one line whose pill leads with the worst note", () => {
    render(
      <ReviewGateCard
        turn={turn({
          blocks: [failedBlock],
          review: {
            blocks: [
              { label: "add_to_cart", blockType: "task", change: "added" },
              {
                label: "send_receipt",
                blockType: "task",
                change: "added",
                neverTested: true,
              },
              { label: "open_page", blockType: "task", change: "changed" },
            ],
            duplicateWrites: [
              {
                blockType: "task",
                blockLabels: ["add_to_cart", "send_receipt"],
              },
            ],
          },
        })}
        pending={false}
        settled="accepted"
        verdict="untested"
        actionsEnabled
        hasProposal
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
      />,
    );

    expect(screen.getByText("Accepted and saved to the workflow")).toBeTruthy();
    // Test failed outranks the warning and the untested block, which the count stands for.
    expect(screen.getByText(/^Test failed/).textContent).toBe(
      "Test failed +2 and 2 more notes",
    );
    expect(screen.queryByTitle("add_to_cart")).toBeNull();
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { expanded: false }));
    expect(screen.getByTitle("add_to_cart")).toBeTruthy();
    expect(screen.getByTitle("send_receipt")).toBeTruthy();
    expect(
      screen.getByText(
        "Add To Cart and Send Receipt write to the same destination.",
      ),
    ).toBeTruthy();
  });
});

describe("ReviewGateCard — source coverage", () => {
  const noop = () => {};

  it("separates a block run against different source from one never run", () => {
    render(
      <ReviewGateCard
        turn={turn({
          review: {
            blocks: [
              {
                label: "sign_in",
                blockType: "code",
                change: "changed",
                neverTested: true,
                coverage: "different_source",
              },
              {
                label: "read_metric",
                blockType: "code",
                change: "added",
                neverTested: true,
                coverage: "never_run",
              },
            ],
            duplicateWrites: [],
          },
        })}
        pending
        verdict="untested"
        actionsEnabled
        hasProposal
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
      />,
    );

    expect(screen.getByText("Different source")).not.toBeNull();
    expect(screen.getByText("Never tested")).not.toBeNull();
  });

  it("says a renamed block is untested under this name, not that it failed", () => {
    render(
      <ReviewGateCard
        turn={turn({
          review: {
            blocks: [
              {
                label: "sign_in_v2",
                blockType: "code",
                change: "added",
                neverTested: true,
                coverage: "unknown",
              },
              {
                label: "read_metric",
                blockType: "code",
                change: "unchanged",
                neverTested: false,
                coverage: "current_source",
              },
            ],
            duplicateWrites: [],
          },
        })}
        pending
        verdict="untested"
        actionsEnabled
        hasProposal
        onAccept={noop}
        onAlwaysAccept={noop}
        onReject={noop}
        onReview={noop}
      />,
    );

    expect(screen.getByText("Not tested under this name")).not.toBeNull();
    expect(screen.queryByText("Never tested")).toBeNull();
    expect(screen.queryByText(/failed/i)).toBeNull();
    expect(screen.queryByText(/invalid/i)).toBeNull();
  });

  it("stays non-committal on a proposal whose turn published no facts", () => {
    expect(
      getReviewGateVerdict(
        turn({
          proposalDisposition: "auto_applicable",
          turnFacts: {
            factsAvailable: false,
            authoredBlockCount: null,
            matchingSourceBlockCount: null,
            evaluationState: null,
            runId: null,
            runCompleted: null,
            terminalCause: null,
            blocksRunThisTurn: null,
            ranCleanOnCurrentSource: false,
          },
        }),
        null,
      ),
    ).toBe("untested");
  });
});
