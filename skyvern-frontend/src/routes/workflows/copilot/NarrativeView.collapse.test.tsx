// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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
  mode: "build",
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
  mode: "build",
  terminal: null,
});

const HEADLINE = "Built and tested the workflow";
const REVIEW_HEADLINE = "Draft needs review";
const REVIEW_TESTED_HEADLINE = "Workflow ready for review";
const LONG_OUTCOME_REASON =
  "The verification challenge kept reappearing after submit, and each retry landed back on the same gate instead of the requested destination page.";
const SHORT_OUTCOME_REASON = "A verification challenge prevented confirmation";

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
    expect(
      screen.getByText("Waiting for the first block to start…"),
    ).toBeTruthy();
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
    expect(screen.getAllByText("add_to_cart")).toHaveLength(1);
  });

  it("appends a truncated not-demonstrated reason to the collapsed subtitle", () => {
    render(
      <NarrativeView
        turn={{
          ...terminalBuildTurn(),
          lastRunOutcome: {
            verdict: "not_demonstrated",
            displayReason: LONG_OUTCOME_REASON,
            activitySeqAtVerdict: 8,
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
      mode: "build",
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
            activitySeqAtVerdict: 9,
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
            activitySeqAtVerdict: 9,
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
            activitySeqAtVerdict: 9,
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
            activitySeqAtVerdict: 9,
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
      mode: "build",
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
