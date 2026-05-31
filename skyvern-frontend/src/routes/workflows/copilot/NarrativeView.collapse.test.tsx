// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { NarrativeView } from "./NarrativeView";
import {
  BlockState,
  EMPTY_NARRATIVE,
  TurnNarrativeState,
} from "./narrativeState";

const completedBlock = (label: string): BlockState => ({
  workflowRunBlockId: `wrb_${label}`,
  label,
  blockType: "navigation",
  state: "completed",
  lastSeenIteration: 1,
  activity: [],
  startedAt: "2026-05-30T00:00:00Z",
  endedAt: "2026-05-30T00:00:10Z",
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

afterEach(() => {
  cleanup();
});

describe("NarrativeView collapse default", () => {
  it("rolls up a terminal turn to the summary card by default", () => {
    render(<NarrativeView turn={terminalBuildTurn()} />);

    expect(screen.getByText(HEADLINE)).toBeTruthy();
    expect(screen.queryByLabelText("Collapse turn")).toBeNull();
  });

  it("keeps the in-flight turn expanded in the detail view", () => {
    render(<NarrativeView turn={inFlightTurn()} />);

    expect(screen.queryByText(HEADLINE)).toBeNull();
    expect(
      screen.getByText("Waiting for the first block to start…"),
    ).toBeTruthy();
  });

  it("expands on click and re-collapses via the collapse control", () => {
    render(<NarrativeView turn={terminalBuildTurn()} />);

    fireEvent.click(screen.getByText(HEADLINE));
    expect(screen.queryByText(HEADLINE)).toBeNull();
    expect(screen.getByLabelText("Collapse turn")).toBeTruthy();

    fireEvent.click(screen.getByLabelText("Collapse turn"));
    expect(screen.getByText(HEADLINE)).toBeTruthy();
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

    fireEvent.click(screen.getByText(HEADLINE));
    expect(screen.queryByText(HEADLINE)).toBeNull();

    rerender(<NarrativeView turn={turn} />);
    expect(screen.queryByText(HEADLINE)).toBeNull();
    expect(screen.getByLabelText("Collapse turn")).toBeTruthy();
  });
});
