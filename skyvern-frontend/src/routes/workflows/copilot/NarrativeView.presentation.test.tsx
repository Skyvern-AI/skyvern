// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { NarrativeView } from "./NarrativeView";
import {
  BlockState,
  EMPTY_NARRATIVE,
  TurnNarrativeState,
} from "./narrativeState";

const completedBlock = (): BlockState => ({
  workflowRunBlockId: "wrb_open_site",
  label: "open_site",
  blockType: "navigation",
  state: "completed",
  lastSeenIteration: 1,
  activity: [],
  startedAt: "2026-05-30T00:00:00Z",
  endedAt: "2026-05-30T00:00:10Z",
});

const structuredTurn = (
  overrides: Partial<TurnNarrativeState> = {},
): TurnNarrativeState => ({
  ...EMPTY_NARRATIVE,
  turnId: "turn-1",
  turnIndex: 0,
  designStarted: true,
  designEnded: true,
  draft: { blockCount: 1, blockLabels: ["open_site"], summary: null },
  blocks: [completedBlock()],
  terminal: "response",
  narrativeSummary: "Built the **navigation** block.",
  startedAt: "2026-05-30T00:00:00Z",
  endedAt: "2026-05-30T00:00:12Z",
  ...overrides,
});

afterEach(cleanup);

describe("NarrativeView structured turn presentation", () => {
  it("uses one detailed reading view without the legacy rollup", () => {
    render(<NarrativeView turn={structuredTurn()} />);

    expect(screen.queryByRole("button", { name: "Collapse turn" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Built the workflow/ }),
    ).toBeNull();
    expect(screen.getByText("navigation", { selector: "strong" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Open Site/ })).toBeTruthy();
  });

  it("uses the same presentation for an in-flight structured turn", () => {
    render(
      <NarrativeView
        turn={structuredTurn({ terminal: null, endedAt: null })}
      />,
    );

    expect(screen.queryByRole("button", { name: "Collapse turn" })).toBeNull();
    expect(screen.getByRole("button", { name: /Open Site/ })).toBeTruthy();
  });

  it("keeps a recorded unconfirmed outcome on its activity row", () => {
    const reason = "The expected destination was not observed.";
    render(
      <NarrativeView
        turn={structuredTurn({
          blocks: [
            {
              ...completedBlock(),
              outcome: "not_demonstrated",
              outcomeReason: reason,
              outcomeRole: "recorded",
            },
          ],
          lastRunOutcome: {
            verdict: "not_demonstrated",
            displayReason: reason,
            role: "recorded",
          },
        })}
      />,
    );

    expect(screen.queryByText("Outcome not confirmed")).toBeNull();
    const activityRow = screen.getByRole("button", { name: /Open Site.*ran/ });
    fireEvent.click(activityRow);
    expect(screen.getByText(reason)).toBeTruthy();
  });

  it("shows a stopped row's run-level reason while the row is collapsed", () => {
    const reason = "The expected destination was not observed.";
    render(
      <NarrativeView
        turn={structuredTurn({
          blocks: [{ ...completedBlock(), state: "stopped" }],
          lastRunOutcome: {
            verdict: "not_demonstrated",
            displayReason: reason,
            role: "recorded",
          },
        })}
      />,
    );

    expect(screen.queryByText("Outcome not confirmed")).toBeNull();
    const activityRow = screen.getByRole("button", {
      name: /Open Site.*stopped/,
    });
    expect(
      screen.getByText(/The expected destination was not observed/),
    ).toBeTruthy();
    expect(activityRow.getAttribute("aria-expanded")).toBe("false");
  });

  it("shows a failed row's run-level reason while the row is collapsed", () => {
    const reason = "The expected destination was not observed.";
    render(
      <NarrativeView
        turn={structuredTurn({
          blocks: [{ ...completedBlock(), state: "failed" }],
          lastRunOutcome: {
            verdict: "not_demonstrated",
            displayReason: reason,
            role: "recorded",
          },
        })}
      />,
    );

    const activityRow = screen.getByRole("button", {
      name: /Open Site.*halted/,
    });
    expect(
      screen.getByText(/The expected destination was not observed/),
    ).toBeTruthy();
    expect(activityRow.getAttribute("aria-expanded")).toBe("false");
  });

  it("does not make a reasonless stopped owner expandable", () => {
    render(
      <NarrativeView
        turn={structuredTurn({
          blocks: [{ ...completedBlock(), state: "stopped" }],
          lastRunOutcome: {
            verdict: "not_demonstrated",
            displayReason: null,
            role: "recorded",
          },
        })}
      />,
    );

    const activityRow = screen.getByRole("button", {
      name: /Open Site.*stopped/,
    });
    expect(activityRow.getAttribute("aria-expanded")).toBeNull();
  });
});
