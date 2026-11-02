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
  NarrativeEvent,
  TurnFacts,
  TurnNarrativeState,
  applyNarrativeEvent,
  hydrateNarrativeFromPayload,
} from "./narrativeState";
import capture from "./narrativeState.hydrationParity.fixture.json";

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
    // The warning is the card's line, not a preview behind a click.
    expect(
      screen.getByRole("button", { name: /Not confirmed Open Site/ }),
    ).toBeTruthy();
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
    expect(
      screen.getByRole("button", { name: /Stopped Open Site/ }),
    ).toBeTruthy();
    expect(
      screen.getByText(/The expected destination was not observed/),
    ).toBeTruthy();
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

    expect(
      screen.getByRole("button", { name: /Failed Open Site/ }),
    ).toBeTruthy();
    expect(
      screen.getByText(/The expected destination was not observed/),
    ).toBeTruthy();
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

    const card = screen.getByRole("button", { name: /Stopped Open Site/ });
    expect(card.getAttribute("aria-expanded")).toBeNull();
  });

  it.each([
    [["wr_left"], "deadline"],
    [[], "deadline"],
    [[], "max_turns"],
  ] as const)(
    "names the runs left running (%j) after the %s limit",
    (leftRunIds, source) => {
      render(
        <NarrativeView
          turn={structuredTurn({
            blocks: [{ ...completedBlock(), state: "unfinished" }],
            budgetExpiry: {
              budgetExpired: true,
              source,
              reportProduced: true,
              stagedDraftId: null,
              drainFingerprint: null,
              leftRunIds: [...leftRunIds],
            },
            turnFacts: {
              factsAvailable: true,
              evaluationState: null,
              runId: "wr_left",
              runCompleted: false,
              terminalCause: null,
              blocksRunThisTurn: 1,
              ranCleanOnCurrentSource: false,
              authoredBlockCount: 1,
              matchingSourceBlockCount: 1,
            },
          })}
        />,
      );

      expect(
        screen.getByRole("button", { name: /Unfinished Open Site/ }),
      ).toBeTruthy();
      expect(
        screen.getByText(/the turn ended before this block finished/),
      ).toBeTruthy();
      expect(screen.queryByText(/Passed|Stopped|Failed/)).toBeNull();
      const runPage = screen.queryByRole("link", { name: /its run page/ });
      if (leftRunIds.length) {
        expect(screen.getByText(/may still be going/)).toBeTruthy();
        expect(runPage?.getAttribute("href")).toBe("/runs/wr_left");
      } else {
        expect(screen.queryByText(/may still be going/)).toBeNull();
        expect(runPage).toBeNull();
      }
    },
  );

  it("links each run the time limit left", () => {
    render(
      <NarrativeView
        turn={structuredTurn({
          blocks: [{ ...completedBlock(), state: "unfinished" }],
          budgetExpiry: {
            budgetExpired: true,
            source: "deadline",
            reportProduced: true,
            stagedDraftId: null,
            drainFingerprint: null,
            leftRunIds: ["wr_a", "wr_b"],
          },
        })}
      />,
    );

    expect(screen.getByText(/test runs may still be going/)).toBeTruthy();
    expect(
      screen
        .getAllByRole("link", { name: /wr_/ })
        .map((link) => link.getAttribute("href")),
    ).toEqual(["/runs/wr_a", "/runs/wr_b"]);
  });
});

// codegen_progress frames are live-only and never persisted: the backend
// streams them while the model writes an authoring tool call's arguments, and
// the tool_call / workflow_draft frames that follow supersede them.
const draftingTurn = (
  frames: string[][],
  generationIds: string[] = [],
): TurnNarrativeState => {
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
      generation_id: generationIds[i],
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

  // A restarted generation opens exactly like a second authoring call, with an
  // empty frame, so only the generation id says the earlier labels were abandoned.
  it("drops an abandoned generation's blocks but keeps every call's within one", () => {
    const frames = [["open_page"], [], ["fill_form"], [], ["extract_results"]];
    const ids = ["gen-a", "gen-a", "gen-a", "gen-b", "gen-b"];
    const { rerender } = render(
      <NarrativeView turn={draftingTurn(frames.slice(0, 3), ids)} />,
    );
    expect(draftingRowText()).toContain("Open Page");
    expect(draftingRowText()).toContain("Fill Form");

    rerender(<NarrativeView turn={draftingTurn(frames, ids)} />);
    const text = draftingRowText();
    expect(text).toContain("Extract Results");
    expect(text).not.toContain("Open Page");
    expect(text).not.toContain("Fill Form");
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
    expect(screen.getByText("Tested the workflow")).toBeTruthy();
  });
});

const at = (second: number) =>
  `2026-05-30T00:01:${String(second).padStart(2, "0")}Z`;

const runCall = (id: string, second: number): NarrativeEvent => ({
  type: "tool_call",
  tool_name: "update_and_run_blocks",
  tool_input: {},
  iteration: second,
  tool_call_id: id,
  timestamp: at(second),
});

const runFailed = (
  id: string,
  second: number,
  detail: string,
): NarrativeEvent => ({
  type: "tool_result",
  tool_name: "update_and_run_blocks",
  success: false,
  summary: "Couldn't complete that step.",
  detail,
  iteration: second,
  tool_call_id: id,
  timestamp: at(second),
});

const blockFrame = (
  runId: string,
  runBlockId: string,
  status: string,
  second: number,
  label = "open_site",
): NarrativeEvent => ({
  type: "block_progress",
  workflow_run_id: runId,
  workflow_run_block_id: runBlockId,
  block_label: label,
  block_type: "code",
  status,
  iteration: second,
  timestamp: at(second),
});

const failedBlock = (
  runId: string,
  runBlockId: string,
  second: number,
  label = "open_site",
): NarrativeEvent[] => [
  blockFrame(runId, runBlockId, "running", second, label),
  blockFrame(runId, runBlockId, "failed", second + 1, label),
];

const failedAction = (
  runBlockId: string,
  response: string,
): NarrativeEvent => ({
  type: "client_block_actions",
  blocks: [
    {
      workflowRunBlockId: runBlockId,
      actions: [
        {
          actionId: `${runBlockId}-a1`,
          label: "Click",
          summary: "Sign in",
          durationMs: 1000,
          failed: true,
          codeLine: null,
          response,
        },
      ],
    },
  ],
  receivedAtMs: 0,
});

const runOutcome = (
  runId: string,
  runBlockIds: string[],
  reason: string,
  second: number,
): NarrativeEvent => ({
  type: "run_outcome",
  workflow_run_id: runId,
  workflow_run_block_ids: runBlockIds,
  block_labels: ["open_site"],
  verdict: "not_demonstrated",
  role: "recorded",
  display_reason: reason,
  iteration: second,
  timestamp: at(second),
});

const playTurn = (events: NarrativeEvent[]): TurnNarrativeState =>
  [
    {
      type: "turn_start",
      turn_id: "turn-1",
      turn_index: 0,
      timestamp: at(0),
    } as NarrativeEvent,
    { type: "design_start", timestamp: at(0) } as NarrativeEvent,
    ...events,
  ].reduce((turn, event) => applyNarrativeEvent(turn, event), EMPTY_NARRATIVE);

const ended = (turn: TurnNarrativeState): TurnNarrativeState => ({
  ...turn,
  terminal: "response",
  endedAt: at(59),
});

const occurrences = (text: string): number =>
  (document.body.textContent ?? "").split(text).length - 1;

const testCards = (): HTMLElement[] =>
  screen.queryAllByTestId("copilot-test-card");

const cardFor = (attempt: string): HTMLElement => {
  const card = testCards().find((c) => c.textContent?.includes(attempt));
  if (card === undefined) throw new Error(`no card reads ${attempt}`);
  return card;
};

const activityLines = (): HTMLElement[] =>
  Array.from(
    document.querySelectorAll<HTMLElement>('[data-activity-line="true"]'),
  );

describe("NarrativeView failed test attempts", () => {
  const firstReason = "The sign-in button never appeared.";

  it("reads a failed attempt as one Copilot is fixing, with its error once wherever it is folded", () => {
    const live = playTurn([
      runCall("call-1", 1),
      ...failedBlock("wr_1", "wrb_1", 2),
      runOutcome("wr_1", ["wrb_1"], firstReason, 4),
      {
        type: "client_block_actions",
        blocks: [
          {
            workflowRunBlockId: "wrb_1",
            actions: [
              {
                actionId: "a1",
                label: "Click",
                summary: "Sign in",
                durationMs: 1000,
                failed: true,
                codeLine: null,
                response: null,
              },
            ],
          },
        ],
        receivedAtMs: 0,
      },
    ]);
    const { rerender } = render(<NarrativeView turn={live} />);

    const card = cardFor("Attempt 1 failed");
    expect(card.dataset.tone).toBe("failed");
    expect(card.textContent).toContain("Copilot is fixing it");
    expect(occurrences("Outcome not confirmed")).toBe(0);
    expect(occurrences(firstReason)).toBe(1);

    fireEvent.click(within(card).getAllByRole("button")[0]!);
    expect(
      within(cardFor("Attempt 1 failed"))
        .getAllByRole("button")[0]!
        .getAttribute("aria-expanded"),
    ).toBe("false");
    expect(occurrences(firstReason)).toBe(1);

    fireEvent.click(activityLines()[0]!);
    expect(testCards()).toHaveLength(0);
    expect(occurrences(`Attempt 1 failed — ${firstReason}`)).toBe(1);

    rerender(<NarrativeView turn={ended(live)} />);
    expect(occurrences("Copilot is fixing it")).toBe(0);
  });

  it("never shows an earlier attempt's reason on a newer attempt before its outcome lands", () => {
    const secondReason = "The form rejected the saved password.";
    const beforeOutcome = playTurn([
      runCall("call-1", 1),
      ...failedBlock("wr_1", "wrb_1", 2),
      runOutcome("wr_1", ["wrb_1"], firstReason, 4),
      runFailed("call-1", 5, "Locator timed out"),
      runCall("call-2", 10),
      ...failedBlock("wr_2", "wrb_2", 11),
    ]);
    const { rerender } = render(<NarrativeView turn={beforeOutcome} />);

    expect(cardFor("Attempt 2 failed").textContent).not.toContain(firstReason);
    expect(occurrences("Outcome not confirmed")).toBe(0);

    rerender(<NarrativeView turn={ended(beforeOutcome)} />);
    expect(occurrences("Outcome not confirmed")).toBe(0);
    expect(occurrences(firstReason)).toBe(
      occurrences(`Attempt 1 failed — ${firstReason}`),
    );

    const afterOutcome = applyNarrativeEvent(
      beforeOutcome,
      runOutcome("wr_2", ["wrb_2"], secondReason, 14),
    );
    rerender(<NarrativeView turn={afterOutcome} />);
    const second = cardFor("Attempt 2 failed");
    expect(second.textContent).toContain(secondReason);
    expect(second.textContent).not.toContain(firstReason);
  });

  it("keeps an error only its failed action carried on the folded card", () => {
    const actionError = "Timed out waiting for the sign-in button.";
    render(
      <NarrativeView
        turn={playTurn([
          runCall("call-1", 1),
          ...failedBlock("wr_1", "wrb_1", 2),
          failedAction(
            "wrb_1",
            `${actionError}\nCall log: waiting for locator`,
          ),
        ])}
      />,
    );
    expect(occurrences(actionError)).toBe(1);

    fireEvent.click(
      within(cardFor("Attempt 1 failed")).getAllByRole("button")[0]!,
    );
    const folded = cardFor("Attempt 1 failed");
    expect(folded.textContent).toContain(actionError);
    expect(folded.textContent).not.toContain("Call log");
    expect(occurrences(actionError)).toBe(1);
  });

  it("puts a run's reason under its last failed block before the design phase starts, over its call's result", () => {
    const runReason = "The run stopped at the confirmation step.";
    const callResult = "Locator timed out on the confirm button.";
    const turn = [
      {
        type: "turn_start",
        turn_id: "turn-1",
        turn_index: 0,
        timestamp: at(0),
      } as NarrativeEvent,
      ...failedBlock("wr_1", "wrb_a", 2, "fill_form"),
      ...failedBlock("wr_1", "wrb_b", 4, "confirm"),
      runOutcome("wr_1", ["wrb_a", "wrb_b"], runReason, 6),
    ].reduce((t, event) => applyNarrativeEvent(t, event), EMPTY_NARRATIVE);
    const { rerender } = render(<NarrativeView turn={turn} />);

    expect(testCards()).toHaveLength(0);
    expect(screen.getByText("Fill Form")).toBeTruthy();
    expect(occurrences(runReason)).toBe(1);
    expect(occurrences("Halted — see run details.")).toBe(1);

    rerender(
      <NarrativeView
        turn={{
          ...turn,
          blocks: turn.blocks.map((b) =>
            b.label === "confirm"
              ? {
                  ...b,
                  activity: [
                    {
                      kind: "tool_result",
                      id: "tr-call-1",
                      text: callResult,
                      iteration: 7,
                    },
                  ],
                }
              : b,
          ),
        }}
      />,
    );
    expect(occurrences(runReason)).toBe(1);
    expect(occurrences(callResult)).toBe(1);
  });

  it("keeps two runs filed under one run call apart", () => {
    const firstRunReason = "The first run never reached the form.";
    const secondRunReason = "The second run was rejected by the form.";
    render(
      <NarrativeView
        turn={playTurn([
          ...failedBlock("wr_x", "wrb_x", 2),
          runOutcome("wr_x", ["wrb_x"], firstRunReason, 4),
          runCall("call-2", 10),
          ...failedBlock("wr_y", "wrb_y", 11),
          runOutcome("wr_y", ["wrb_y"], secondRunReason, 13),
        ])}
      />,
    );

    expect(testCards()).toHaveLength(2);
    expect(cardFor("Attempt 1 failed").textContent).toContain(firstRunReason);
    const second = cardFor("Attempt 2 failed");
    expect(second.textContent).toContain(secondRunReason);
    expect(second.textContent).not.toContain(firstRunReason);
    expect(occurrences(firstRunReason)).toBe(1);
  });

  it("numbers loop iterations of one run call as one attempt", () => {
    render(
      <NarrativeView
        turn={playTurn([
          runCall("call-1", 1),
          ...failedBlock("wr_1", "wrb_1a", 2),
          ...failedBlock("wr_1", "wrb_1b", 4),
          runFailed("call-1", 6, "Locator timed out"),
          runCall("call-2", 10),
          ...failedBlock("wr_2", "wrb_2", 11),
        ])}
      />,
    );
    activityLines()
      .filter((line) => line.getAttribute("aria-expanded") === "false")
      .forEach((line) => fireEvent.click(line));

    const words = testCards().map(
      (card) => card.textContent?.match(/Attempt \d+ failed/)?.[0],
    );
    expect(words).toEqual([
      "Attempt 1 failed",
      "Attempt 1 failed",
      "Attempt 2 failed",
    ]);
    expect(
      testCards().map((card) =>
        card.textContent?.includes("Copilot is fixing it"),
      ),
    ).toEqual([false, false, true]);
  });

  it("reads the turn's verdict on a failed block only when it is that block's run's", () => {
    const verdict = "The confirmation page never loaded.";
    const turn = playTurn([
      runCall("call-1", 1),
      ...failedBlock("wr_1", "wrb_1", 2),
      runOutcome("wr_1", [], verdict, 4),
    ]);
    const { rerender } = render(<NarrativeView turn={turn} />);
    expect(cardFor("Attempt 1 failed").textContent).toContain(verdict);

    rerender(
      <NarrativeView
        turn={{
          ...turn,
          lastRunOutcome: { ...turn.lastRunOutcome!, workflowRunId: "wr_0" },
        }}
      />,
    );
    expect(occurrences(verdict)).toBe(0);
  });

  it.each([
    ["the activity log", true],
    ["the block list", false],
  ])(
    "never numbers or shares a verdict across runs saved without a run id in %s",
    (_view, designStarted) => {
      const verdict = "The confirmation page never loaded.";
      const played = playTurn([
        runCall("call-1", 1),
        ...failedBlock("wr_1", "wrb_1", 2),
        runFailed("call-1", 4, "Locator timed out"),
        runCall("call-2", 10),
        ...failedBlock("wr_2", "wrb_2", 11),
        runOutcome("wr_2", [], verdict, 13),
      ]);
      const legacy = ended({
        ...played,
        designStarted,
        lastRunOutcome: { ...played.lastRunOutcome!, workflowRunId: undefined },
        blocks: played.blocks.map((b) => ({ ...b, workflowRunId: undefined })),
      });
      render(<NarrativeView turn={legacy} />);
      const fold = screen.queryByRole("button", { name: /^Worked through/ });
      if (fold !== null) fireEvent.click(fold);
      activityLines()
        .filter((line) => line.getAttribute("aria-expanded") === "false")
        .forEach((line) => fireEvent.click(line));

      expect(occurrences("Attempt")).toBe(0);
      expect(occurrences(verdict)).toBe(1);
    },
  );

  it("reads the owning card's run reason over its action text, live and after a reload", () => {
    const runReason = "The first attempt failed.";
    const actionError = "Timed out waiting for the submit button.";
    const updates = capture.updates as unknown as NarrativeEvent[];
    const payload = capture.payload as unknown as Record<string, unknown>;
    const failedAt = updates.findIndex(
      (e) =>
        e.type === "block_progress" &&
        e.workflow_run_block_id === "wrb_first" &&
        e.status === "failed",
    );
    const live = [
      ...updates.slice(0, failedAt + 1),
      failedAction("wrb_first", actionError),
      ...updates.slice(failedAt + 1),
      {
        type: "response",
        workflow_copilot_chat_id: "chat-replay",
        message: "done",
        response_time: "2026-09-08T00:00:10Z",
        proposal_disposition: "review_tested",
        turn_id: "turn-replay",
        narrative_payload: payload,
      } as NarrativeEvent,
    ].reduce(applyNarrativeEvent, { ...EMPTY_NARRATIVE });
    expect(live.blocks[1]!.recordedActions).toHaveLength(1);
    const reloaded = hydrateNarrativeFromPayload(payload)!;

    const surfaces = (turn: TurnNarrativeState) => {
      const { unmount } = render(<NarrativeView turn={turn} />);
      const seen: string[] = [document.body.textContent ?? ""];
      fireEvent.click(screen.getByRole("button", { name: /^Worked through/ }));
      seen.push(document.body.textContent ?? "");
      fireEvent.click(
        activityLines().find((line) =>
          line.textContent?.includes("Block 19 failed"),
        )!,
      );
      seen.push(cardFor("Attempt 1 failed").textContent ?? "");
      fireEvent.click(
        within(cardFor("Attempt 1 failed")).getAllByRole("button")[0]!,
      );
      seen.push(cardFor("Attempt 1 failed").textContent ?? "");
      unmount();
      return seen.map((text) => [
        text.split(runReason).length - 1,
        text.includes(actionError),
      ]);
    };

    const liveSurfaces = surfaces(live);
    expect(surfaces(reloaded)).toEqual(liveSurfaces);
    expect(liveSurfaces).toEqual([
      [1, false],
      [1, false],
      [1, false],
      [1, false],
    ]);
  });

  it("keeps a run's reason on its last failed block, off its siblings, live and after a reload", () => {
    const runReason = "The run stopped at the confirmation step.";
    const actionError = "The username field was missing.";
    const live = playTurn([
      runCall("call-1", 1),
      ...failedBlock("wr_1", "wrb_a", 2, "fill_form"),
      failedAction("wrb_a", actionError),
      ...failedBlock("wr_1", "wrb_b", 4, "confirm"),
      failedAction("wrb_b", "Timed out on the confirm button."),
      runOutcome("wr_1", ["wrb_a", "wrb_b"], runReason, 6),
      runFailed("call-1", 7, runReason),
    ]);
    const reloaded = ended({
      ...live,
      blocks: live.blocks.map((b) => ({ ...b, recordedActions: undefined })),
    });
    const { rerender } = render(<NarrativeView turn={live} />);
    const [fillForm, confirm] = testCards();
    expect(fillForm!.textContent).toContain(actionError);
    expect(fillForm!.textContent).not.toContain(runReason);
    expect(confirm!.textContent).toContain(runReason);
    expect(occurrences(runReason)).toBe(1);
    expect(occurrences("Timed out on the confirm button.")).toBe(0);

    rerender(<NarrativeView turn={ended(live)} />);
    expect(occurrences(`Confirm: Attempt 1 failed — ${runReason}`)).toBe(1);
    expect(occurrences(`Fill Form: Attempt 1 failed — ${actionError}`)).toBe(1);

    rerender(<NarrativeView turn={reloaded} />);
    expect(occurrences(`Confirm: Attempt 1 failed — ${runReason}`)).toBe(1);
    expect(occurrences("Fill Form: Attempt 1 failed")).toBe(1);
    expect(occurrences(runReason)).toBe(1);
    const fold = screen.queryByRole("button", { name: /^Worked through/ });
    if (fold !== null) fireEvent.click(fold);
    activityLines()
      .filter((line) => line.getAttribute("aria-expanded") === "false")
      .forEach((line) => fireEvent.click(line));
    expect(cardFor("Fill Form").textContent).toContain(
      "Halted — see run details.",
    );
    expect(cardFor("Fill Form").textContent).not.toContain(runReason);
    expect(cardFor("Confirm").textContent).toContain(runReason);
  });

  it("keeps the failed action's full error under Show details beneath the run's reason", () => {
    const runReason = "The run stopped at the sign-in step.";
    const actionError =
      "Timed out waiting for the sign-in button.\nCall log: waiting for locator";
    render(
      <NarrativeView
        turn={playTurn([
          runCall("call-1", 1),
          ...failedBlock("wr_1", "wrb_1", 2),
          failedAction("wrb_1", actionError),
          runOutcome("wr_1", ["wrb_1"], runReason, 4),
        ])}
      />,
    );
    const card = cardFor("Attempt 1 failed");
    expect(occurrences(runReason)).toBe(1);
    expect(card.textContent).not.toContain("Timed out waiting");

    fireEvent.click(within(card).getByRole("button", { name: /Show details/ }));
    expect(cardFor("Attempt 1 failed").textContent).toContain(actionError);
    expect(occurrences(runReason)).toBe(1);
  });

  it("reads a Test button run as a failed attempt while it runs and after its row lands", () => {
    const reason = "The sign-in button never appeared.";
    const callId = "call_test_end_to_end_turn-1";
    const duringRun = [
      {
        type: "turn_start",
        turn_id: "turn-1",
        turn_index: 0,
        timestamp: at(0),
      } as NarrativeEvent,
      { type: "design_start", timestamp: at(0) } as NarrativeEvent,
      ...failedBlock("wr_1", "wrb_1", 2),
      runOutcome("wr_1", ["wrb_1"], reason, 4),
    ].reduce((t, event) => applyNarrativeEvent(t, event), EMPTY_NARRATIVE);
    const { rerender } = render(<NarrativeView turn={duringRun} />);
    expect(cardFor("Attempt 1 failed").dataset.tone).toBe("failed");
    expect(occurrences("halted")).toBe(0);
    expect(occurrences(reason)).toBe(1);

    const rowLanded = [
      {
        type: "tool_call",
        tool_name: "run_blocks_and_collect_debug",
        tool_input: {},
        iteration: 0,
        tool_call_id: callId,
        timestamp: at(1),
      } as NarrativeEvent,
      {
        type: "tool_result",
        tool_name: "run_blocks_and_collect_debug",
        success: false,
        summary: "Couldn't complete that step.",
        detail: reason,
        workflow_run_id: "wr_1",
        iteration: 0,
        tool_call_id: callId,
        timestamp: at(5),
      } as NarrativeEvent,
    ].reduce((t, event) => applyNarrativeEvent(t, event), duringRun);
    rerender(<NarrativeView turn={rowLanded} />);
    expect(testCards()).toHaveLength(1);
    expect(cardFor("Attempt 1 failed").dataset.tone).toBe("failed");
    expect(occurrences("Outcome not confirmed")).toBe(0);
    expect(occurrences(reason)).toBe(1);
  });

  it("never calls a failed run with no block on screen unconfirmed", () => {
    const reason = "The workflow could not start without its login parameter.";
    const facts = (runCompleted: boolean): TurnFacts => ({
      factsAvailable: true,
      authoredBlockCount: 1,
      matchingSourceBlockCount: 1,
      evaluationState: "not_demonstrated",
      runId: "wr_1",
      runCompleted,
      terminalCause: null,
      blocksRunThisTurn: 0,
      recordedFailure: reason,
      ranCleanOnCurrentSource: false,
    });
    const { rerender } = render(
      <NarrativeView
        turn={structuredTurn({ blocks: [], turnFacts: facts(false) })}
      />,
    );
    expect(occurrences("Outcome not confirmed")).toBe(0);
    expect(occurrences(`Run failed — ${reason}`)).toBe(1);

    rerender(
      <NarrativeView
        turn={structuredTurn({ blocks: [], turnFacts: facts(true) })}
      />,
    );
    expect(occurrences(`Outcome not confirmed — ${reason}`)).toBe(1);
  });
});
