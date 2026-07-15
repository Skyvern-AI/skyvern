// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NarrativeView } from "./NarrativeView";
import {
  ActivityEntry,
  BlockState,
  EMPTY_NARRATIVE,
  TurnNarrativeState,
  hydrateNarrativeFromPayload,
} from "./narrativeState";

afterEach(() => {
  cleanup();
});

const activityEntry = (
  overrides: Partial<ActivityEntry> & Pick<ActivityEntry, "id" | "kind">,
): ActivityEntry => ({
  text: "…",
  iteration: 0,
  ...overrides,
});

const runningBlock = (overrides: Partial<BlockState> = {}): BlockState => ({
  workflowRunBlockId: "wrb_1",
  label: "block_1",
  blockType: "task",
  state: "running",
  lastSeenIteration: 0,
  activity: [],
  startedAt: "2026-06-10T00:00:05Z",
  endedAt: null,
  ...overrides,
});

const liveExploreTurn = (): TurnNarrativeState => ({
  ...EMPTY_NARRATIVE,
  turnId: "turn-1",
  turnIndex: 0,
  mode: "build",
  designStarted: true,
  terminal: null,
  designActivity: [
    activityEntry({
      id: "tc-1",
      kind: "tool_call",
      toolName: "navigate_browser",
      displayLabel: "Opening page",
    }),
  ],
});

const testActiveTurn = (): TurnNarrativeState => ({
  ...EMPTY_NARRATIVE,
  turnId: "turn-1",
  turnIndex: 0,
  mode: "build",
  designStarted: true,
  designEnded: true,
  terminal: null,
  draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
  blocks: [runningBlock()],
  designActivity: [
    activityEntry({
      id: "tc-1",
      kind: "tool_call",
      toolName: "navigate_browser",
      displayLabel: "Opening page",
    }),
    activityEntry({
      id: "tc-2",
      kind: "tool_call",
      toolName: "update_and_run_blocks",
      displayLabel: "Testing workflow",
    }),
  ],
});

describe("NarrativeView — phase checklist (SKY-11970)", () => {
  it("PIN: a live design turn renders an active Explore site row nesting its tool-activity stream", () => {
    render(<NarrativeView turn={liveExploreTurn()} uxV1 />);
    // Active rows render inert (no aria-expanded button) — status reaches
    // screen readers via the sr-only word instead.
    const exploreLabel = screen.getByText("Explore site");
    expect(exploreLabel.closest("button")).toBeNull();
    const exploreRow = exploreLabel.closest("div")!;
    expect(exploreRow.querySelector(".animate-spin")).toBeTruthy();
    expect(exploreRow.textContent).toContain("Active");
    expect(screen.getByText("Opening page")).toBeTruthy();
  });

  it("auto-collapses a phase once it's no longer active, and re-opens on click", () => {
    const { rerender } = render(
      <NarrativeView turn={liveExploreTurn()} uxV1 />,
    );
    expect(screen.getByText("Opening page")).toBeTruthy();

    rerender(<NarrativeView turn={testActiveTurn()} uxV1 />);
    const exploreRow = screen.getByRole("button", { name: /Explore site/ });
    expect(exploreRow.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("Opening page")).toBeNull();
    expect(screen.getByText("1 step")).toBeTruthy();

    fireEvent.click(exploreRow);
    expect(exploreRow.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("Opening page")).toBeTruthy();
  });

  it("excludes the active row from toggling — renders inert, not a clickable button", () => {
    render(<NarrativeView turn={testActiveTurn()} uxV1 />);
    const testLabel = screen.getByText("Test-run");
    // The whole point: an active row must not be a focusable no-op button
    // (keyboard/screen-reader trap) — it's a plain div.
    expect(testLabel.closest("button")).toBeNull();
    const testRow = testLabel.closest("div")!;
    const before = testRow.parentElement!.innerHTML;
    fireEvent.click(testRow);
    expect(testRow.parentElement!.innerHTML).toBe(before);
  });

  it("hosts FBlockRun exactly once, inside the Test-run nest — no double-render with the legacy flat list", () => {
    render(<NarrativeView turn={testActiveTurn()} uxV1 />);
    // uxV1 humanizes the primary block label ("block_1" -> "Block 1").
    expect(screen.getAllByText("Block 1")).toHaveLength(1);
  });

  it("a11y: nest-less rows render a plain div, not a button", () => {
    render(<NarrativeView turn={liveExploreTurn()} uxV1 />);
    const doneLabel = screen.getByText("Done");
    expect(doneLabel.closest("button")).toBeNull();
  });

  it("names the redraft iteration in the shimmered Draft placeholder after a failed verify", () => {
    const redraftTurn: TurnNarrativeState = {
      ...EMPTY_NARRATIVE,
      turnId: "turn-1",
      turnIndex: 0,
      mode: "build",
      designStarted: true,
      designEnded: true,
      terminal: null,
      authoringCount: 1,
      activitySeq: 3,
      draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
      blocks: [
        runningBlock({
          state: "completed",
          endedAt: "2026-06-10T00:00:10Z",
        }),
      ],
      lastRunOutcome: {
        verdict: "not_demonstrated",
        displayReason: "outcome not confirmed",
        activitySeqAtVerdict: 2,
      },
      designActivity: [
        activityEntry({
          id: "tc-1",
          kind: "tool_call",
          toolName: "navigate_browser",
        }),
        activityEntry({
          id: "tc-2",
          kind: "tool_call",
          toolName: "update_and_run_blocks",
        }),
        activityEntry({ id: "n-1", kind: "narration" }),
      ],
    };
    render(<NarrativeView turn={redraftTurn} uxV1 />);
    expect(
      screen.getByText(
        /Draft v2 — revising after failed verify: outcome not confirmed/,
      ),
    ).toBeTruthy();
  });

  it("REGRESSION PIN: never shows redraft copy on an ordinary first build with no prior verdict (Codex catch)", () => {
    const firstBuildDraftActive: TurnNarrativeState = {
      ...EMPTY_NARRATIVE,
      turnId: "turn-1",
      turnIndex: 0,
      mode: "build",
      designStarted: true,
      terminal: null,
      authoringCount: 1,
      activitySeq: 1,
      lastRunOutcome: null,
      designActivity: [
        activityEntry({
          id: "tc-1",
          kind: "tool_call",
          toolName: "update_workflow",
        }),
      ],
    };
    render(<NarrativeView turn={firstBuildDraftActive} uxV1 />);
    expect(screen.getByText("Writing the workflow code…")).toBeTruthy();
    expect(screen.queryByText(/revising after failed verify/)).toBeNull();
  });

  it("REGRESSION PIN: Test-run stays expandable when blocks exist even with an empty test-activity bucket (Codex catch)", () => {
    const historyRowNoTestActivity: TurnNarrativeState = {
      ...EMPTY_NARRATIVE,
      turnId: "turn-1",
      turnIndex: 0,
      mode: "build",
      designStarted: true,
      designEnded: true,
      terminal: "response",
      draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
      blocks: [
        runningBlock({ state: "completed", endedAt: "2026-06-10T00:00:10Z" }),
      ],
      designActivity: [],
    };
    render(<NarrativeView turn={historyRowNoTestActivity} uxV1 />);
    // Terminal turns roll up by default, but the checklist already renders
    // inline inside the RollupCard — no need to expand into DetailView.
    const testRow = screen.getByRole("button", { name: /Test-run/ });
    expect(testRow.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(testRow);
    expect(testRow.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTitle("Highlight block_1 on canvas")).toBeTruthy();
  });

  it("hydration no-replay pin: a reloaded terminal build turn renders all stubs — zero spinners, zero open nests, no pending timers", () => {
    vi.useFakeTimers();
    const payload = {
      turnId: "turn-1",
      turnIndex: 0,
      mode: "build",
      designStarted: true,
      designEnded: true,
      draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
      blocks: [
        {
          workflowRunBlockId: "wrb_1",
          label: "block_1",
          blockType: "task",
          state: "completed",
          lastSeenIteration: 0,
          activity: [],
          startedAt: "2026-06-10T00:00:00Z",
          endedAt: "2026-06-10T00:00:10Z",
        },
      ],
      terminal: "response",
      terminalMessage: "Done.",
      narrativeSummary: "Built and tested the workflow.",
      priorBlockCount: null,
      designActivity: [
        {
          kind: "tool_call",
          text: "Opening page…",
          iteration: 0,
          toolName: "navigate_browser",
          displayLabel: "Opening page",
          id: "tc-1",
        },
        {
          kind: "tool_call",
          text: "Testing workflow…",
          iteration: 1,
          toolName: "update_and_run_blocks",
          displayLabel: "Testing workflow",
          id: "tc-2",
        },
      ],
      startedAt: "2026-06-10T00:00:00Z",
      endedAt: "2026-06-10T00:00:10Z",
    };
    const hydrated = hydrateNarrativeFromPayload(payload)!;

    render(<NarrativeView turn={hydrated} uxV1 />);

    expect(screen.getByText("Explore site")).toBeTruthy();
    expect(screen.getByText("Draft code")).toBeTruthy();
    expect(screen.getByText("Test-run")).toBeTruthy();
    expect(screen.getByText("Done")).toBeTruthy();
    expect(document.querySelectorAll(".animate-spin").length).toBe(0);
    expect(document.querySelectorAll('[aria-expanded="true"]').length).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
    vi.useRealTimers();
  });

  it("shows no checklist rows for a clarify terminal turn that never drafted anything", () => {
    const clarifyTurn: TurnNarrativeState = {
      ...EMPTY_NARRATIVE,
      turnId: "turn-2",
      turnIndex: 0,
      mode: "clarify",
      designStarted: true,
      terminal: "response",
      responseType: "ASK_QUESTION",
      narrativeSummary: "Which login should I use?",
      draft: null,
      blocks: [],
    };
    render(<NarrativeView turn={clarifyTurn} uxV1 />);
    expect(screen.queryByText("Explore site")).toBeNull();
    expect(screen.queryByText("Draft code")).toBeNull();
    expect(screen.queryByText("Test-run")).toBeNull();
  });

  it("flag-off parity: uxV1 absent renders today's FDesignRow, zero checklist rows", () => {
    render(<NarrativeView turn={liveExploreTurn()} />);
    expect(screen.getByText("Designing the workflow")).toBeTruthy();
    expect(screen.queryByText("Explore site")).toBeNull();
    expect(screen.queryByText("Draft code")).toBeNull();
    expect(screen.queryByText("Test-run")).toBeNull();
  });

  it("humanizes the primary block label under uxV1, keeping the raw label in a title attribute", () => {
    const turn = testActiveTurn();
    turn.blocks = [
      runningBlock({
        state: "completed",
        endedAt: "2026-06-10T00:00:10Z",
        label: "extract_first_comments_from_top_three_posts_v2",
      }),
    ];
    render(<NarrativeView turn={turn} uxV1 />);
    const label = screen.getByText(
      "Extract First Comments From Top Three Posts",
    );
    expect(label.getAttribute("title")).toBe(
      "extract_first_comments_from_top_three_posts_v2",
    );
    expect(
      screen.queryByText("extract_first_comments_from_top_three_posts_v2"),
    ).toBeNull();
  });

  it("flag-off parity: block labels render raw, unhumanized, when uxV1 is absent", () => {
    render(<NarrativeView turn={testActiveTurn()} />);
    expect(screen.getByText("block_1")).toBeTruthy();
    expect(screen.queryByText("Block 1")).toBeNull();
  });
});

describe("NarrativeView — narrator content condensing (SKY-11971)", () => {
  const retriedBlockActivity: ActivityEntry[] = [
    activityEntry({
      id: "tc-x1",
      kind: "tool_call",
      toolName: "extract",
      displayLabel: "Extracting",
    }),
    activityEntry({
      id: "tr-x1",
      kind: "tool_result",
      toolName: "extract",
      success: false,
      text: "no results found",
    }),
    activityEntry({
      id: "tc-x2",
      kind: "tool_call",
      toolName: "extract",
      displayLabel: "Extracting",
    }),
    activityEntry({
      id: "tr-x2",
      kind: "tool_result",
      toolName: "extract",
      success: true,
      text: "top 5 titles + links",
    }),
  ];

  const retriedTurn = (): TurnNarrativeState => ({
    ...testActiveTurn(),
    blocks: [runningBlock({ activity: retriedBlockActivity })],
  });

  it("checklist (uxV1): folds the block's failed-then-retried tool activity into one row with an attempt count", () => {
    render(<NarrativeView turn={retriedTurn()} uxV1 />);
    expect(screen.queryByText("no results found")).toBeNull();
    expect(screen.getByText("top 5 titles + links")).toBeTruthy();
    expect(screen.getByText(/2 attempts/)).toBeTruthy();
  });

  it("legacy (uxV1 absent): renders every raw call/result row unfolded, exactly as before", () => {
    render(<NarrativeView turn={retriedTurn()} />);
    expect(screen.getByText("no results found")).toBeTruthy();
    expect(screen.getByText("top 5 titles + links")).toBeTruthy();
    expect(screen.queryByText(/attempts/)).toBeNull();
  });
});
