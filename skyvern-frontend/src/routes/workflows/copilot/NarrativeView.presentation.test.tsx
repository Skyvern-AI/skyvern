// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { NarrativeView } from "./NarrativeView";
import {
  BlockState,
  EMPTY_NARRATIVE,
  TurnNarrativeState,
  applyNarrativeEvent,
  hydrateNarrativeFromPayload,
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

// codegen_progress frames are live-only and never persisted: the backend
// streams them while the model writes an authoring tool call's arguments, and
// the tool_call / workflow_draft frames that follow supersede them.
const draftingTurn = (frames: string[][]): TurnNarrativeState => {
  let turn = applyNarrativeEvent(EMPTY_NARRATIVE, {
    type: "turn_start",
    turn_id: "turn-1",
    turn_index: 0,
    timestamp: "2026-05-30T00:00:00Z",
  });
  turn = applyNarrativeEvent(turn, {
    type: "design_start",
    timestamp: "2026-05-30T00:00:00Z",
  });
  frames.forEach((blocksDrafted, i) => {
    turn = applyNarrativeEvent(turn, {
      type: "codegen_progress",
      tool_name: "update_and_run_blocks",
      blocks_drafted: blocksDrafted,
      chars_streamed: (i + 1) * 400,
      iteration: 1,
      timestamp: `2026-05-30T00:00:0${i + 1}Z`,
    });
  });
  return turn;
};

// The log stamps every row with its own id; the drafting row's is stable.
const draftingRow = (): HTMLElement | null =>
  document.querySelector('[data-activity-row-id="codegen-progress"]');

const draftingRowText = (): string => {
  const row = draftingRow();
  if (row === null) throw new Error("no drafting row rendered");
  return row.textContent ?? "";
};

describe("NarrativeView drafting row (codegen_progress)", () => {
  // The frames carry the raw authored identifiers, so the row is also where
  // `open_page` would leak into the chat if it were rendered verbatim.
  it("names the blocks, humanized, in the order the frames drafted them", () => {
    render(
      <NarrativeView
        turn={draftingTurn([
          [],
          ["open_page"],
          ["open_page", "fill_form"],
          ["open_page", "fill_form", "extract_results"],
        ])}
      />,
    );

    const text = draftingRowText();
    expect(text).not.toContain("open_page");
    expect(text).toContain("Open Page");
    expect(text).toContain("Fill Form");
    expect(text).toContain("Extract Results");
    expect(text.indexOf("Open Page")).toBeLessThan(text.indexOf("Fill Form"));
    expect(text.indexOf("Fill Form")).toBeLessThan(
      text.indexOf("Extract Results"),
    );
  });

  // One generation can carry more than one authoring call. `blocks_drafted` is
  // cumulative only WITHIN a call — the adapter keys its state by output_index
  // and opens each new call with an empty frame — so a second call must not
  // erase what the first one drafted.
  it("keeps the blocks an earlier authoring call drafted in the same generation", () => {
    render(
      <NarrativeView
        turn={draftingTurn([
          ["open_page"],
          ["open_page", "fill_form"],
          [],
          ["extract_results"],
        ])}
      />,
    );

    const text = draftingRowText();
    expect(text).toContain("Open Page");
    expect(text).toContain("Fill Form");
    expect(text).toContain("Extract Results");
  });

  it("keeps the existing placeholder when no frames arrive", () => {
    render(
      <NarrativeView
        turn={applyNarrativeEvent(
          applyNarrativeEvent(EMPTY_NARRATIVE, {
            type: "turn_start",
            turn_id: "turn-1",
            turn_index: 0,
            timestamp: "2026-05-30T00:00:00Z",
          }),
          { type: "design_start", timestamp: "2026-05-30T00:00:00Z" },
        )}
      />,
    );

    expect(draftingRow()).toBeNull();
    expect(
      screen.getByText("Copilot is working on your request…"),
    ).toBeTruthy();
  });

  it("does not outlive the live frames", () => {
    const drafting = draftingTurn([["open_page"], ["open_page", "fill_form"]]);
    const { rerender } = render(<NarrativeView turn={drafting} />);
    expect(draftingRowText()).toContain("Fill Form");

    // The authoring call the frames were describing is now executing: its own
    // row reports the work, so the drafting row must not stay beside it.
    rerender(
      <NarrativeView
        turn={applyNarrativeEvent(drafting, {
          type: "tool_call",
          tool_name: "update_and_run_blocks",
          tool_input: {},
          iteration: 1,
          tool_call_id: "call-1",
          timestamp: "2026-05-30T00:00:05Z",
        })}
      />,
    );
    expect(draftingRow()).toBeNull();

    // A reload rebuilds the turn from the persisted payload, which carries no
    // live frames — what the model wrote is reported by the persisted authoring
    // step, and the drafting row cannot come back stuck and empty beside it.
    const reloaded = hydrateNarrativeFromPayload({
      turnId: "turn-1",
      turnIndex: 0,
      terminal: "response",
      draft: { blockCount: 2, blockLabels: ["open_page", "fill_form"] },
      designActivity: [
        {
          kind: "tool_result",
          id: "tr-call-1",
          text: "Wrote the workflow code",
          toolName: "update_and_run_blocks",
          iteration: 1,
          success: true,
          timestamp: "2026-05-30T00:00:09Z",
        },
      ],
    });
    rerender(<NarrativeView turn={reloaded!} />);
    expect(draftingRow()).toBeNull();
    expect(screen.getByText("Wrote the workflow code")).toBeTruthy();
  });
});
