// @vitest-environment jsdom

import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NarrativeView } from "./NarrativeView";
import {
  ActivityEntry,
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
  codeLine: null,
  response: null,
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

// Forty words, the ceiling the backend sanitizer now enforces — long enough
// to wrap past four lines in the narrowest copilot pane.
const REASON =
  "Checking whether the invoice archive asks for a login before the run tries to open it, because the goal needs the paid receipts rather than the public summary page that anyone browsing the site can already reach today";

const toolCall = (
  id: string,
  iteration: number,
  toolName = "navigate_browser",
): ActivityEntry => ({
  kind: "tool_call",
  text: `Calling tool ${id}`,
  iteration,
  toolName,
  id: `tc-${id}`,
});

const toolResult = (id: string, iteration: number): ActivityEntry => ({
  kind: "tool_result",
  text: `Tool ${id} finished`,
  iteration,
  toolName: "navigate_browser",
  success: true,
  id: `tr-${id}`,
});

const narration = (
  iteration: number,
  receivedAtMs: number | undefined,
): ActivityEntry => ({
  kind: "narration",
  text: REASON,
  iteration,
  id: `n-${iteration}-t`,
  receivedAtMs,
});

const narrationTurn = (
  designActivity: ActivityEntry[],
): TurnNarrativeState => ({
  ...EMPTY_NARRATIVE,
  turnId: "turn-n",
  turnIndex: 0,
  designStarted: true,
  designActivity,
  terminal: null,
  startedAt: "2026-06-10T00:00:00Z",
});

const reason = () => document.querySelector('[data-testid="copilot-reason"]');
const caret = () =>
  document.querySelector(
    '[data-testid="copilot-reason"] > span[aria-hidden="true"]',
  );
const hidden = () =>
  document.querySelector('[data-testid="copilot-reason"] > .text-transparent');

describe("NarrativeView — narration reveal", () => {
  it("reveals a live narration progressively and retires the caret in place", () => {
    render(
      <NarrativeView
        turn={narrationTurn([toolCall("1", 1), narration(1, NOW - 500)])}
      />,
    );

    expect(caret()!.className).toContain("animate-pulse");
    expect(hidden()!.textContent!.length).toBeGreaterThan(0);
    expect(hidden()!.textContent!.length).toBeLessThan(REASON.length);

    act(() => {
      vi.advanceTimersByTime(60_000);
    });

    expect(hidden()!.textContent).toBe("");
    expect(caret()!.className).toContain("opacity-0");
    expect(reason()!.textContent).toContain(REASON);
  });

  it("renders a narration with no arrival stamp complete on first render", () => {
    render(
      <NarrativeView
        turn={narrationTurn([toolCall("1", 1), narration(1, undefined)])}
      />,
    );

    expect(hidden()!.textContent).toBe("");
    expect(caret()!.className).toContain("opacity-0");
    expect(reason()!.textContent).toContain(REASON);
  });

  it("keeps a step's sentence above it once a later step starts", () => {
    render(
      <NarrativeView
        turn={narrationTurn([
          toolCall("1", 1),
          toolResult("1", 1),
          narration(1, NOW),
          toolCall("2", 2, "edit_block"),
        ])}
      />,
    );

    act(() => {
      vi.advanceTimersByTime(60_000);
    });

    expect(hidden()!.textContent).toBe("");
    expect(caret()!.className).toContain("opacity-0");
    expect(reason()!.textContent).toContain(REASON);
    // The whole sentence wraps; nothing cuts it to an ellipsis.
    expect(reason()!.className).not.toContain("line-clamp");
  });
});
