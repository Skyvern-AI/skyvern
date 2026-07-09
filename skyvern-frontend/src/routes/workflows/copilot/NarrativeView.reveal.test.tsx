// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NarrativeView } from "./NarrativeView";
import {
  BlockState,
  EMPTY_NARRATIVE,
  RecordedActionSummary,
  TurnNarrativeState,
} from "./narrativeState";

const NOW = new Date("2026-06-10T00:00:00Z").getTime();

const action = (
  actionId: string,
  overrides: Partial<RecordedActionSummary> = {},
): RecordedActionSummary => ({
  actionId,
  label: `Action ${actionId}`,
  summary: null,
  durationMs: 200,
  failed: false,
  ...overrides,
});

const verifyingBlockWithActions = (
  actions: RecordedActionSummary[],
  recordedActionsAt: number,
): BlockState => ({
  workflowRunBlockId: "wrb_1",
  label: "block_1",
  blockType: "code",
  state: "completed",
  outcome: "evaluating",
  lastSeenIteration: 1,
  activity: [],
  startedAt: "2026-06-10T00:00:00Z",
  endedAt: "2026-06-10T00:00:05Z",
  recordedActions: actions,
  recordedActionsAt,
});

const inFlightTurnWithBlock = (block: BlockState): TurnNarrativeState => ({
  ...EMPTY_NARRATIVE,
  turnId: "turn-1",
  turnIndex: 0,
  mode: "build",
  blocks: [block],
  terminal: null,
  startedAt: "2026-06-10T00:00:00Z",
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("NarrativeView — recorded action reveal", () => {
  it("reveals recorded actions one by one as time advances (regression pin)", () => {
    const actions = [action("a1"), action("a2")];
    render(
      <NarrativeView
        turn={inFlightTurnWithBlock(verifyingBlockWithActions(actions, NOW))}
      />,
    );

    // Only the first action has started revealing; the second hasn't
    // appeared at all yet — old code renders neither, ever.
    expect(screen.getByText("Action a1")).toBeTruthy();
    expect(screen.queryByText("Action a2")).toBeNull();
    expect(document.querySelectorAll(".animate-spin").length).toBe(1);

    act(() => {
      vi.advanceTimersByTime(250);
    });
    expect(screen.getByText("Action a2")).toBeTruthy();
    expect(document.querySelectorAll(".animate-spin").length).toBe(1);

    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(document.querySelectorAll(".animate-spin").length).toBe(0);
  });

  it("shows everything immediately with no in-progress row when recordedActionsAt is far in the past", () => {
    const actions = [action("a1"), action("a2"), action("a3")];
    render(
      <NarrativeView
        turn={inFlightTurnWithBlock(
          verifyingBlockWithActions(actions, NOW - 60_000),
        )}
      />,
    );

    expect(screen.getByText("Action a1")).toBeTruthy();
    expect(screen.getByText("Action a2")).toBeTruthy();
    expect(screen.getByText("Action a3")).toBeTruthy();
    expect(document.querySelectorAll(".animate-spin").length).toBe(0);
  });

  it("survives a real unmount/remount (rollup collapse -> expand) without restarting the schedule", () => {
    const actions = [action("a1"), action("a2")];
    const failedBlock: BlockState = {
      ...verifyingBlockWithActions(actions, NOW),
      state: "failed",
      outcome: undefined,
    };
    const terminalTurn: TurnNarrativeState = {
      ...inFlightTurnWithBlock(failedBlock),
      terminal: "response",
      terminalMessage: "Halted.",
      endedAt: "2026-06-10T00:00:10Z",
    };

    render(<NarrativeView turn={terminalTurn} />);
    // A terminal turn defaults to the rolled-up summary card — the block
    // row (and its actions) are not mounted at all yet.
    expect(screen.queryByText("Action a1")).toBeNull();

    act(() => {
      vi.advanceTimersByTime(350);
    });

    // Expand into the detail view — FBlockRun mounts for the first time here.
    fireEvent.click(screen.getByRole("button", { name: /Run halted/ }));

    // 350ms had already elapsed before this fresh mount: one action already
    // done, the second mid-reveal — not restarted from zero.
    expect(screen.getByText("Action a1")).toBeTruthy();
    expect(screen.getByText("Action a2")).toBeTruthy();
    expect(document.querySelectorAll(".animate-spin").length).toBe(1);
  });

  it("renders no recorded-action rows when the block has none (byte-identical to today)", () => {
    const block: BlockState = {
      workflowRunBlockId: "wrb_1",
      label: "block_1",
      blockType: "navigation",
      state: "running",
      lastSeenIteration: 1,
      activity: [],
      startedAt: "2026-06-10T00:00:00Z",
      endedAt: null,
    };
    render(<NarrativeView turn={inFlightTurnWithBlock(block)} />);

    expect(screen.getByText("Working…")).toBeTruthy();
    expect(
      document.querySelectorAll(".animate-copilot-row-flash-success").length,
    ).toBe(0);
    expect(
      document.querySelectorAll(".animate-copilot-row-flash-error").length,
    ).toBe(0);
  });
});
