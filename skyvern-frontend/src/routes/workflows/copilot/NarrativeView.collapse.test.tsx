// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { NarrativeView } from "./NarrativeView";
import {
  BlockState,
  EMPTY_NARRATIVE,
  TurnNarrativeState,
  hydrateNarrativeFromPayload,
} from "./narrativeState";

const completedBlock = (
  label: string,
  workflowRunBlockId = `wrb_${label}`,
): BlockState => ({
  workflowRunBlockId,
  label,
  blockType: "navigation",
  state: "completed",
  lastSeenIteration: 1,
  activity: [],
  startedAt: "2026-05-30T00:00:00Z",
  endedAt: "2026-05-30T00:00:10Z",
});

const failedBlock = (
  label: string,
  workflowRunBlockId = `wrb_failed_${label}`,
): BlockState => ({
  ...completedBlock(label, workflowRunBlockId),
  state: "failed",
});

const terminalBuildTurn = (): TurnNarrativeState => ({
  ...EMPTY_NARRATIVE,
  turnId: "turn-1",
  turnIndex: 0,
  designStarted: true,
  designEnded: true,
  draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
  blocks: [completedBlock("block_1")],
  terminal: "response",
  narrativeSummary: "Built it.",
  startedAt: "2026-05-30T00:00:00Z",
  endedAt: "2026-05-30T00:00:12Z",
});

const inFlightTurn = (): TurnNarrativeState => ({
  ...EMPTY_NARRATIVE,
  turnId: "turn-2",
  turnIndex: 1,
  terminal: null,
});

const HEADLINE = "Built the workflow";
const REVIEW_HEADLINE = "Draft needs review";
const REVIEW_TESTED_HEADLINE = "Workflow ready for review";
const LONG_OUTCOME_REASON =
  "The verification challenge kept reappearing after submit, and each retry landed back on the same gate instead of the requested destination page.";
const SHORT_OUTCOME_REASON = "A verification challenge prevented confirmation";

const interimRunTurn = (): TurnNarrativeState => ({
  ...inFlightTurn(),
  blocks: [
    {
      ...completedBlock("block_1"),
      outcome: "not_demonstrated",
      outcomeRole: "interim_build_test",
      outcomeReason: SHORT_OUTCOME_REASON,
    },
  ],
  lastRunOutcome: {
    verdict: "not_demonstrated",
    role: "interim_build_test",
    displayReason: SHORT_OUTCOME_REASON,
  },
});

afterEach(() => {
  cleanup();
});

describe("NarrativeView collapse default", () => {
  it("rolls up a terminal turn to the summary card by default", () => {
    render(<NarrativeView turn={terminalBuildTurn()} />);

    expect(screen.getByText(HEADLINE)).toBeTruthy();
    expect(
      screen
        .getByRole("button", { name: new RegExp(HEADLINE) })
        .getAttribute("aria-expanded"),
    ).toBe("false");
  });

  it("does not present an unverified review proposal as built and tested", () => {
    render(
      <NarrativeView
        turn={{
          ...terminalBuildTurn(),
          proposalDisposition: "review_untested",
          terminalMessage:
            "I reached the requested browser state, but the reusable workflow still needs a clean verification run before it is ready.",
          narrativeSummary:
            "I reached the requested browser state, but the reusable workflow still needs a clean verification run before it is ready.",
        }}
      />,
    );

    expect(screen.getByText(REVIEW_HEADLINE)).toBeTruthy();
    expect(screen.queryByText(HEADLINE)).toBeNull();
  });

  it("does not present a tested review proposal with a success badge", () => {
    render(
      <NarrativeView
        turn={{
          ...terminalBuildTurn(),
          proposalDisposition: "review_tested",
          // One block, run clean on current source — what backs a tested review proposal.
          turnFacts: {
            factsAvailable: true,
            authoredBlockCount: 1,
            matchingSourceBlockCount: 1,
            evaluationState: null,
            runId: "wr_1",
            runCompleted: true,
            terminalCause: null,
            blocksRunThisTurn: 1,
            ranCleanOnCurrentSource: true,
          },
          narrativeSummary: "Workflow ready for review.",
        }}
      />,
    );

    const summaryButton = screen.getByRole("button", {
      name: new RegExp(REVIEW_TESTED_HEADLINE),
    });
    expect(summaryButton.textContent).toContain("!");
    expect(summaryButton.textContent).not.toContain("✓");
    expect(screen.queryByText(HEADLINE)).toBeNull();
  });

  it("keeps the in-flight turn expanded in the detail view", () => {
    render(<NarrativeView turn={inFlightTurn()} />);

    expect(screen.queryByText(HEADLINE)).toBeNull();
    expect(screen.getByText("Working…")).toBeTruthy();
  });

  it("renders an interim completed row as neutral", () => {
    render(<NarrativeView turn={interimRunTurn()} />);

    const row = screen.getByTitle("Highlight block_1 on canvas");
    expect(row.textContent).toContain("ran");
    expect(row.textContent).not.toContain("!");
    expect(row.textContent).not.toContain("✓");
    expect(screen.queryByText(/Outcome not confirmed/)).toBeNull();
    expect(screen.queryByText(SHORT_OUTCOME_REASON)).toBeNull();
  });

  it.each([undefined, "adjudicated" as const])(
    "keeps role=%s not-demonstrated rows amber",
    (outcomeRole) => {
      const block: BlockState = {
        ...completedBlock("block_1"),
        outcome: "not_demonstrated",
        outcomeReason: SHORT_OUTCOME_REASON,
        ...(outcomeRole ? { outcomeRole } : {}),
      };
      render(<NarrativeView turn={{ ...inFlightTurn(), blocks: [block] }} />);

      const row = screen.getByTitle("Highlight block_1 on canvas");
      expect(row.textContent).toContain("!");
      expect(row.textContent).not.toContain("✓");
      expect(screen.getByText(/Outcome not confirmed/)).toBeTruthy();
      expect(screen.getByText(new RegExp(SHORT_OUTCOME_REASON))).toBeTruthy();
    },
  );

  it("hydrates an interim outcome into the same neutral row treatment", () => {
    const hydrated = hydrateNarrativeFromPayload({
      turnId: "turn-hydrated-interim",
      turnIndex: 0,
      blocks: [
        {
          ...completedBlock("block_1"),
          outcome: "not_demonstrated",
          outcomeRole: "interim_build_test",
          outcomeReason: SHORT_OUTCOME_REASON,
        },
      ],
      terminal: null,
    })!;
    render(<NarrativeView turn={hydrated} />);

    const row = screen.getByTitle("Highlight block_1 on canvas");
    expect(row.textContent).toContain("ran");
    expect(row.textContent).not.toContain("!");
    expect(row.textContent).not.toContain("✓");
    expect(screen.queryByText(/Outcome not confirmed/)).toBeNull();
    expect(screen.queryByText(SHORT_OUTCOME_REASON)).toBeNull();
  });

  it("expands via the summary card and re-collapses via the labeled control", () => {
    render(<NarrativeView turn={terminalBuildTurn()} />);
    const head = () =>
      screen.getByRole("button", { name: new RegExp(HEADLINE) });

    expect(head().getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(head());
    const collapse = screen.getByRole("button", { name: "Collapse turn" });
    expect(screen.queryByText(HEADLINE)).toBeNull();

    fireEvent.click(collapse);
    expect(head().getAttribute("aria-expanded")).toBe("false");
  });

  it("collapses on the in-flight to terminal transition", () => {
    const { rerender } = render(<NarrativeView turn={inFlightTurn()} />);
    expect(screen.queryByText(HEADLINE)).toBeNull();

    rerender(<NarrativeView turn={terminalBuildTurn()} />);
    expect(screen.getByText(HEADLINE)).toBeTruthy();
  });

  it("preserves a user's expand override across re-renders", () => {
    const turn = terminalBuildTurn();
    const { rerender } = render(<NarrativeView turn={turn} />);

    fireEvent.click(screen.getByRole("button", { name: new RegExp(HEADLINE) }));
    expect(screen.getByRole("button", { name: "Collapse turn" })).toBeTruthy();
    expect(screen.queryByText(HEADLINE)).toBeNull();

    rerender(<NarrativeView turn={turn} />);
    expect(screen.getByRole("button", { name: "Collapse turn" })).toBeTruthy();
    expect(screen.queryByText(HEADLINE)).toBeNull();
  });

  it("summarizes the latest retry attempt for duplicate block labels", () => {
    render(
      <NarrativeView
        turn={{
          ...terminalBuildTurn(),
          blocks: [
            completedBlock("open_site", "wrb_open_first"),
            failedBlock("add_to_cart", "wrb_add_first"),
            completedBlock("open_site", "wrb_open_retry"),
            completedBlock("add_to_cart", "wrb_add_retry"),
            completedBlock("confirm_cart", "wrb_confirm_retry"),
          ],
        }}
      />,
    );

    expect(screen.getByText(HEADLINE)).toBeTruthy();
    expect(screen.queryByText("Run halted")).toBeNull();
    expect(screen.queryByText("Halted")).toBeNull();
    const changedSection = screen.getByText("What changed").parentElement!;
    expect(within(changedSection).getAllByText("Add To Cart")).toHaveLength(1);
  });

  it("appends a truncated not-demonstrated reason to the collapsed subtitle", () => {
    render(
      <NarrativeView
        turn={{
          ...terminalBuildTurn(),
          lastRunOutcome: {
            verdict: "not_demonstrated",
            displayReason: LONG_OUTCOME_REASON,
          },
        }}
      />,
    );

    const expectedPreview = `${LONG_OUTCOME_REASON.slice(0, 137).trimEnd()}...`;
    const head = screen.getByRole("button", { name: new RegExp(HEADLINE) });
    expect(head.textContent).toContain(
      `Outcome not confirmed: ${expectedPreview}`,
    );
    expect(head.textContent).not.toContain(LONG_OUTCOME_REASON);
  });

  it("derives the collapsed subtitle reason from hydrated not-demonstrated block outcomes", () => {
    const hydrated = hydrateNarrativeFromPayload({
      turnId: "turn-hydrated",
      turnIndex: 0,
      responseType: "REPLY",
      designStarted: true,
      designEnded: true,
      draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
      blocks: [
        {
          ...completedBlock("block_1"),
          outcome: "not_demonstrated",
          outcomeReason: LONG_OUTCOME_REASON,
        },
      ],
      terminal: "response",
      terminalMessage: "Built it.",
      narrativeSummary: "Built it.",
      startedAt: "2026-05-30T00:00:00Z",
      endedAt: "2026-05-30T00:00:12Z",
    })!;
    const expectedPreview = `${LONG_OUTCOME_REASON.slice(0, 137).trimEnd()}...`;
    render(<NarrativeView turn={hydrated} />);

    const head = screen.getByRole("button", { name: new RegExp(HEADLINE) });
    expect(head.textContent).toContain(
      `Outcome not confirmed: ${expectedPreview}`,
    );
    expect(head.textContent).not.toContain(LONG_OUTCOME_REASON);
  });

  it("does not append a duplicate not-confirmed reason when closing prose already includes it", () => {
    render(
      <NarrativeView
        turn={{
          ...terminalBuildTurn(),
          narrativeSummary:
            "Could not confirm completion. Reason: a   verification challenge prevented confirmation.",
          lastRunOutcome: {
            verdict: "not_demonstrated",
            displayReason: SHORT_OUTCOME_REASON,
          },
        }}
      />,
    );

    const head = screen.getByRole("button", { name: new RegExp(HEADLINE) });
    expect(head.textContent).toContain(
      "Reason: a   verification challenge prevented confirmation.",
    );
    expect(head.textContent).not.toContain(
      `Outcome not confirmed: ${SHORT_OUTCOME_REASON}`,
    );
  });

  it("does not append a duplicate when closing prose carries only a truncated preview of the reason", () => {
    const truncatedPreview = `${LONG_OUTCOME_REASON.slice(0, 137).trimEnd()}...`;
    render(
      <NarrativeView
        turn={{
          ...terminalBuildTurn(),
          narrativeSummary: `Could not confirm completion. Reason: ${truncatedPreview}`,
          lastRunOutcome: {
            verdict: "not_demonstrated",
            displayReason: LONG_OUTCOME_REASON,
          },
        }}
      />,
    );

    const head = screen.getByRole("button", { name: new RegExp(HEADLINE) });
    expect(head.textContent).toContain(`Reason: ${truncatedPreview}`);
    expect(head.textContent).not.toContain("Outcome not confirmed:");
  });

  it("appends not-confirmed reason when closing prose does not include it", () => {
    render(
      <NarrativeView
        turn={{
          ...terminalBuildTurn(),
          narrativeSummary: "Built it.",
          lastRunOutcome: {
            verdict: "not_demonstrated",
            displayReason: SHORT_OUTCOME_REASON,
          },
        }}
      />,
    );

    const head = screen.getByRole("button", { name: new RegExp(HEADLINE) });
    expect(head.textContent).toContain("Built it.");
    expect(head.textContent).toContain(
      `Outcome not confirmed: ${SHORT_OUTCOME_REASON}`,
    );
  });

  it("uses only the not-confirmed segment when there is no closing prose", () => {
    render(
      <NarrativeView
        turn={{
          ...terminalBuildTurn(),
          narrativeSummary: null,
          terminalMessage: null,
          lastRunOutcome: {
            verdict: "not_demonstrated",
            displayReason: SHORT_OUTCOME_REASON,
          },
        }}
      />,
    );

    const head = screen.getByRole("button", { name: new RegExp(HEADLINE) });
    expect(head.textContent).toContain(
      `Outcome not confirmed: ${SHORT_OUTCOME_REASON}`,
    );
    expect(head.textContent).not.toContain("Built it.");
  });

  it("does not append a not-demonstrated reason when no verdict signal is present", () => {
    render(<NarrativeView turn={terminalBuildTurn()} />);
    const head = screen.getByRole("button", { name: new RegExp(HEADLINE) });
    expect(head.textContent).not.toContain("Outcome not confirmed:");
    expect(head.textContent).not.toContain("verification challenge");
  });

  it("keeps hydrated turns with neither signal byte-identical to legacy subtitle behavior", () => {
    const hydrated = hydrateNarrativeFromPayload({
      turnId: "turn-hydrated-legacy",
      turnIndex: 0,
      responseType: "REPLY",
      designStarted: true,
      designEnded: true,
      draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
      blocks: [{ ...completedBlock("block_1") }],
      terminal: "response",
      terminalMessage: "Built it.",
      narrativeSummary: "Built it.",
      startedAt: "2026-05-30T00:00:00Z",
      endedAt: "2026-05-30T00:00:12Z",
    })!;
    render(<NarrativeView turn={hydrated} />);
    const head = screen.getByRole("button", { name: new RegExp(HEADLINE) });
    expect(head.textContent).toContain("Built it.");
    expect(head.textContent).not.toContain("Outcome not confirmed:");
  });
});

const pureAskTurn = (): TurnNarrativeState => ({
  ...EMPTY_NARRATIVE,
  turnId: "turn-ask",
  turnIndex: 0,
  responseType: "ASK_QUESTION",
  terminal: "response",
  narrativeSummary: "Which login should I use?",
});

const SCOUT_NARRATION = "Checked the login page for SSO options.";

const askAfterScoutingTurn = (): TurnNarrativeState => ({
  ...pureAskTurn(),
  turnId: "turn-ask-scouted",
  designStarted: true,
  designActivity: [
    { kind: "narration", text: SCOUT_NARRATION, iteration: 0, id: "n-1" },
  ],
});

describe("NarrativeView rollup expand affordance", () => {
  it("renders a pure needs-input ask as plain, non-expandable prose", () => {
    render(<NarrativeView turn={pureAskTurn()} />);

    expect(screen.getByTestId("copilot-terminal-prose")).toBeTruthy();
    expect(
      screen.getByTestId("copilot-terminal-prose-visual").textContent,
    ).toBe("Which login should I use?");
    expect(screen.queryByRole("button")).toBeNull();
    expect(document.querySelector("[aria-expanded]")).toBeNull();
  });

  it("does not box a question just because background scouting was narrated", () => {
    render(<NarrativeView turn={askAfterScoutingTurn()} />);

    expect(screen.getByTestId("copilot-terminal-prose")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByText(SCOUT_NARRATION)).toBeNull();
  });

  it("still shows the expand chevron for a build turn with blocks", () => {
    render(<NarrativeView turn={terminalBuildTurn()} />);

    const head = screen.getByRole("button", { name: new RegExp(HEADLINE) });
    expect(head.getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(head);
    expect(screen.getByRole("button", { name: "Collapse turn" })).toBeTruthy();
  });

  it("makes later block evidence inspectable after a prose-only ask", () => {
    const { rerender } = render(<NarrativeView turn={pureAskTurn()} />);
    expect(screen.queryByRole("button")).toBeNull();

    rerender(
      <NarrativeView
        turn={{ ...pureAskTurn(), blocks: [completedBlock("block_1")] }}
      />,
    );
    expect(screen.getByRole("button")).toBeTruthy();
  });
});

describe("a stopped block stays inspectable", () => {
  const stoppedTurn = (): TurnNarrativeState => ({
    ...terminalBuildTurn(),
    cancelled: true,
    blocks: [
      {
        ...completedBlock("log_in", "wrb_stopped_log_in"),
        state: "stopped",
        activity: [
          {
            kind: "tool_result",
            text: "opened the sign-in page",
            iteration: 1,
            id: "act-1",
            success: true,
          },
        ],
      },
    ],
  });

  // The collapsed rollup is the default view, so a stopped block has to be
  // named there too — a failed block gets its own section, and dropping the
  // stopped one made it vanish from the summary entirely.
  it("names the stopped block in the collapsed rollup", () => {
    render(<NarrativeView turn={stoppedTurn()} />);

    const stoppedSection = screen.getByText("Stopped", {
      selector: "div",
    }).parentElement!;
    expect(within(stoppedSection).getByText("Log In")).toBeTruthy();
    expect(screen.queryByText("Halted")).toBeNull();
  });

  // A non-solo run row keeps its block cards in the collapsed body, so the log
  // is not a substitute for the summary list on a finished turn.
  it("still names a stopped block on the activity-log surface", () => {
    render(<NarrativeView turn={stoppedTurn()} />);

    expect(screen.getByText("Stopped", { selector: "div" })).toBeTruthy();
  });

  // A stop is not a failure, so the row must not auto-open and shout — but what
  // the block did before the stop still has to be reachable.
  it("can be expanded to reveal what ran before the stop", () => {
    render(<NarrativeView turn={stoppedTurn()} />);

    fireEvent.click(screen.getByRole("button", { name: /Stopped/ }));

    // Not auto-opened: a stop does not shout the way a failure does.
    expect(screen.queryByText("opened the sign-in page")).toBeNull();

    fireEvent.click(screen.getByTitle("Highlight log_in on canvas"));

    expect(screen.getByText("opened the sign-in page")).toBeTruthy();
  });

  // A two-block turn is the case a one-block fixture hides: the run row is not a
  // solo block, so its cards sit in the collapsed body and name nothing there.
  it("names a failed block on a two-block turn, on both surfaces", () => {
    const twoBlockTurn = (): TurnNarrativeState => ({
      ...terminalBuildTurn(),
      designActivity: [
        {
          kind: "tool_result",
          toolName: "update_and_run_blocks",
          text: "Ran the blocks",
          success: true,
          iteration: 0,
          id: "tr-run",
        },
      ],
      blocks: [completedBlock("log_in"), failedBlock("download")],
    });

    const legacy = render(<NarrativeView turn={twoBlockTurn()} />);
    expect(screen.getAllByText(/download/i).length).toBeGreaterThan(0);
    legacy.unmount();

    render(<NarrativeView turn={twoBlockTurn()} />);
    expect(screen.getAllByText(/download/i).length).toBeGreaterThan(0);
  });

  // The write/run row the design shows titled renders as a block card, which
  // previously discarded the narrator title entirely.
  it("titles a solo block row with the narrator label, without losing the block name", () => {
    const narratedRunTurn = (): TurnNarrativeState => ({
      ...terminalBuildTurn(),
      designActivity: [
        {
          kind: "tool_result",
          toolName: "update_and_run_blocks",
          text: "Ran the blocks",
          success: true,
          iteration: 0,
          id: "tr-run",
        },
        {
          kind: "narration",
          text: "Checking the saved steps survive a real run",
          iteration: 0,
          activeLabel: "Running it",
          outcomeLabel: "Rewrote the download step",
          id: "n-run",
        },
      ],
      blocks: [completedBlock("download_report")],
    });

    render(<NarrativeView turn={narratedRunTurn()} />);

    expect(screen.getByText("Rewrote the download step")).toBeTruthy();
    // The block itself is still named by the rollup's own list.
    expect(screen.getAllByText(/download.report/i).length).toBeGreaterThan(0);
  });
});
