// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

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

const testActiveTurn = (): TurnNarrativeState => ({
  ...EMPTY_NARRATIVE,
  turnId: "turn-1",
  turnIndex: 0,
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

  it("folds retry status while retaining the exact failed attempt as evidence", () => {
    render(<NarrativeView turn={retriedTurn()} />);
    expect(screen.getByText("no results found")).toBeTruthy();
    expect(screen.getByText("top 5 titles + links")).toBeTruthy();
    expect(screen.getByText(/2 attempts/)).toBeTruthy();
  });
});

// A finished turn with more than one step folds them under one header.
const openFold = () =>
  fireEvent.click(screen.getByRole("button", { name: /^Worked through/ }));

const stepLines = (): HTMLElement[] =>
  Array.from(document.querySelectorAll<HTMLElement>("[data-activity-line]"));

// What a sighted reader sees on a line, without the screen-reader words.
const visibleText = (el: Element): string => {
  const clone = el.cloneNode(true) as HTMLElement;
  clone.querySelectorAll(".sr-only").forEach((n) => n.remove());
  return (clone.textContent ?? "").replace(/\u00a0/g, " ");
};

const repairLoopTurn = (): TurnNarrativeState => ({
  ...EMPTY_NARRATIVE,
  turnId: "turn-1",
  turnIndex: 0,
  designStarted: true,
  designEnded: true,
  terminal: "response",
  draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
  blocks: [
    runningBlock({ state: "completed", endedAt: "2026-06-10T00:00:10Z" }),
  ],
  designActivity: [
    activityEntry({
      id: "tr-1",
      kind: "tool_result",
      toolName: "navigate_browser",
      text: "Opened the sign-in page",
      success: true,
    }),
    activityEntry({
      id: "tr-2",
      kind: "tool_result",
      toolName: "update_and_run_blocks",
      text: "The submit button stayed disabled after filling the form",
      success: false,
      iteration: 1,
    }),
    activityEntry({
      id: "tr-3",
      kind: "tool_result",
      toolName: "update_workflow",
      text: "Saved 2 blocks",
      success: true,
      iteration: 2,
    }),
  ],
});

describe("NarrativeView — activity log", () => {
  it("folds a finished turn under one header that counts its failed tests", () => {
    render(<NarrativeView turn={repairLoopTurn()} />);
    expect(screen.queryByText("Explore site")).toBeNull();
    const fold = screen.getByRole("button", { name: /^Worked through/ });
    expect(fold.textContent).toBe("Worked through 3 steps · 1 failed test");
    expect(fold.getAttribute("aria-expanded")).toBe("false");
    expect(stepLines()).toHaveLength(0);

    openFold();
    expect(stepLines()).toHaveLength(3);
  });

  it("keeps a block's unconfirmed outcome visible while its turn is folded", () => {
    const turn = repairLoopTurn();
    render(
      <NarrativeView
        turn={{
          ...turn,
          blocks: [
            {
              ...turn.blocks[0]!,
              outcome: "not_demonstrated",
              outcomeReason: "No confirmation page appeared",
            },
          ],
        }}
      />,
    );
    expect(stepLines()).toHaveLength(0);
    expect(screen.getByText(/No confirmation page appeared/)).toBeTruthy();
  });

  it("names a step the narrator never titled by what its calls did", () => {
    render(<NarrativeView turn={{ ...repairLoopTurn(), terminal: null }} />);
    expect(stepLines().map(visibleText)).toEqual([
      "1 browser action",
      "Tested the workflow · attempt failed",
      "Updated the workflow",
    ]);
  });

  it("a failed run keeps the server's reason one click deep", () => {
    render(<NarrativeView turn={repairLoopTurn()} />);
    openFold();
    const error = "The submit button stayed disabled after filling the form";
    expect(screen.queryByText(error)).toBeNull();

    fireEvent.click(stepLines()[1]!);
    expect(screen.getByText(error).className).toContain("rose");
  });

  it("a live test reads as one card per block run, in place of its call", () => {
    const turn = testActiveTurn();
    turn.designActivity = [
      activityEntry({
        id: "tc-2",
        kind: "tool_call",
        toolName: "update_and_run_blocks",
        displayLabel: 'Editing and testing block "block_1"',
        timestamp: "2026-06-10T00:00:01Z",
        codeDiffs: [
          { label: "block_1", added: 3, removed: 2, patch: "-old\n+new" },
        ],
      }),
    ];
    turn.blocks = [
      runningBlock({
        recordedActions: [
          {
            actionId: "a1",
            label: "Goto URL",
            summary: "Open the sign-in page",
            durationMs: 800,
            failed: false,
            codeLine: null,
            response: null,
          },
        ],
        recordedActionsAt: 0,
      }),
    ];
    render(<NarrativeView turn={turn} />);

    const card = screen.getByTestId("copilot-test-card");
    expect(card.dataset.tone).toBe("running");
    expect(visibleText(card)).toContain("Testing Block 1");
    expect(visibleText(card)).toContain("Open the sign-in page");
    expect(visibleText(card)).toContain("0.8s");
    // The card replaces the call row, the diff row and the flat block line.
    expect(screen.queryByText(/Editing and testing block/)).toBeNull();
    expect(screen.queryByText("Active in Live Browser")).toBeNull();
    expect(screen.queryByText("Working…")).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Expand code changes/ }),
    ).toBeNull();
    // Its spinner is the one loader; the step line above does not repeat it.
    expect(screen.queryByText("in progress")).toBeNull();

    expect(screen.queryByText("+new")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Code change/ }));
    expect(screen.getByText("+new")).toBeTruthy();
  });

  it("a retried test reads one card per attempt, the fix in the attempt that ran it", () => {
    const turn = repairLoopTurn();
    const firstAttempt = activityEntry({
      id: "tr-1",
      kind: "tool_result",
      toolName: "update_and_run_blocks",
      text: "Failed: the page never loaded",
      success: false,
      timestamp: "2026-06-10T00:00:08Z",
      codeDiffs: [{ label: "block_1", added: 4, removed: 0, patch: "+old" }],
    });
    const failedBlock = runningBlock({
      workflowRunBlockId: "wrb_1",
      state: "failed",
      // First polled already failed, so it has no start of its own.
      startedAt: null,
      endedAt: "2026-06-10T00:00:07Z",
    });

    // Until the retry reaches the patched block, its new patch stays off the
    // failed card, even while it runs another block first.
    render(
      <NarrativeView
        turn={{
          ...turn,
          terminal: null,
          blocks: [
            failedBlock,
            runningBlock({
              workflowRunBlockId: "wrb_0",
              label: "block_0",
              startedAt: "2026-06-10T00:00:10Z",
            }),
          ],
          designActivity: [
            activityEntry({
              id: "tc-2",
              kind: "tool_call",
              toolName: "update_and_run_blocks",
              attempts: 2,
              priorFailures: [firstAttempt],
              timestamp: "2026-06-10T00:00:09Z",
              codeDiffs: [
                { label: "block_1", added: 2, removed: 0, patch: "+new" },
              ],
            }),
          ],
        }}
      />,
    );
    const pendingCards = screen.getAllByTestId("copilot-test-card");
    expect(pendingCards).toHaveLength(2);
    for (const card of pendingCards) {
      expect(within(card).queryByText("Code change")).toBeNull();
    }
    expect(
      screen.getByRole("button", { name: /Expand code changes/ }),
    ).toBeTruthy();
    cleanup();

    turn.designActivity = [
      activityEntry({
        id: "tr-2",
        kind: "tool_result",
        toolName: "update_and_run_blocks",
        text: "Ran 1 block",
        success: true,
        attempts: 2,
        priorFailures: [firstAttempt],
        timestamp: "2026-06-10T00:00:23Z",
        codeDiffs: [
          { label: "block_1", added: 10, removed: 0, patchDropped: true },
        ],
      }),
    ];
    turn.blocks = [
      failedBlock,
      runningBlock({
        workflowRunBlockId: "wrb_2",
        state: "completed",
        outcome: "demonstrated",
        startedAt: "2026-06-10T00:00:10Z",
        endedAt: "2026-06-10T00:00:22Z",
      }),
    ];
    render(<NarrativeView turn={turn} />);
    fireEvent.click(stepLines()[0]!);

    const [failedCard, passedCard] = screen.getAllByTestId("copilot-test-card");
    expect(failedCard!.getAttribute("data-tone")).toBe("failed");
    expect(
      within(failedCard!).getByText("Failed: the page never loaded"),
    ).toBeTruthy();
    expect(within(failedCard!).queryByText("Code change")).toBeNull();

    const header = within(passedCard!).getByRole("button", {
      name: /Passed Block 1/,
    });
    expect(header.getAttribute("aria-expanded")).toBe("false");
    expect(visibleText(header)).not.toContain("attempts");
    fireEvent.click(header);
    // The patch was dropped, so the change reads as counts with nothing to open.
    expect(within(passedCard!).getByText("Code change")).toBeTruthy();
    expect(
      within(passedCard!).queryByRole("button", { name: /Code change/ }),
    ).toBeNull();
  });

  it("a run row still calling claims no result", () => {
    const turn = repairLoopTurn();
    turn.terminal = null;
    turn.designActivity = [
      activityEntry({
        id: "tc-run",
        kind: "tool_call",
        toolName: "update_and_run_blocks",
        text: "Testing workflow",
        iteration: 0,
      }),
    ];
    turn.blocks = [];
    render(<NarrativeView turn={turn} />);

    const [line] = stepLines();
    expect(visibleText(line!)).toBe("Testing the workflow");
  });

  it("the kind reaches a screen reader as a word", () => {
    render(<NarrativeView turn={repairLoopTurn()} />);
    openFold();
    expect(screen.getAllByText(/Looked at the page ·/).length).toBeGreaterThan(
      0,
    );
    expect(screen.getAllByText(/Ran it ·/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Wrote code ·/).length).toBeGreaterThan(0);
  });

  const browseEntry = (i: number, toolName: string, text: string) =>
    activityEntry({
      id: `tr-b${i}`,
      kind: "tool_result",
      toolName,
      text,
      success: true,
      iteration: i,
    });

  const groupedBrowseTurn = (): TurnNarrativeState => ({
    ...EMPTY_NARRATIVE,
    turnId: "turn-1",
    turnIndex: 0,
    designStarted: true,
    terminal: null,
    designActivity: [
      browseEntry(0, "navigate_browser", "Opened the sign-in page"),
      browseEntry(1, "fill_credential_field", "Filled the saved login"),
      browseEntry(2, "click", "Clicked 'text=Invoices'"),
      browseEntry(3, "set_work_plan", "Planned three steps"),
    ],
  });

  const twoInFlightTurn = (): TurnNarrativeState => ({
    ...EMPTY_NARRATIVE,
    turnId: "turn-1",
    turnIndex: 0,
    designStarted: true,
    terminal: null,
    designActivity: [
      browseEntry(0, "navigate_browser", "Opened the sign-in page"),
      browseEntry(1, "get_page_evidence", "Read the form state"),
      activityEntry({
        id: "tc-3",
        kind: "tool_call",
        toolName: "update_workflow",
        displayLabel: "Saving blocks",
        iteration: 2,
      }),
      browseEntry(4, "click_element", "Checked the cart"),
      activityEntry({
        id: "tc-5",
        kind: "tool_call",
        toolName: "navigate_browser",
        displayLabel: "Opening page",
        iteration: 3,
      }),
    ],
  });

  it("an open step lists its calls, and a call opens to its saved result", () => {
    render(<NarrativeView turn={groupedBrowseTurn()} />);
    expect(visibleText(stepLines()[0]!)).toBe(
      "2 browser actions, used a saved login, updated its plan",
    );

    // The newest row stays open through the gap between calls, each call's
    // result reading beside it on one line until the call is opened.
    const result = screen.getByText("Opened the sign-in page");
    expect(result.className).toContain("truncate");

    fireEvent.click(screen.getByRole("button", { name: /Opening page/ }));
    expect(screen.getByText("Opened the sign-in page").className).toContain(
      "whitespace-pre-wrap",
    );
  });

  it("repeated identical calls read as one line with a count", () => {
    const turn = groupedBrowseTurn();
    turn.designActivity = [
      browseEntry(0, "evaluate", "Evaluated JavaScript: returned 3 items"),
      browseEntry(1, "evaluate", "Evaluated JavaScript: returned 3 items"),
      browseEntry(2, "evaluate", "Evaluated JavaScript: returned 5 items"),
    ].map((entry) => ({ ...entry, displayLabel: "Inspecting page" }));
    render(<NarrativeView turn={turn} />);

    const calls = screen.getAllByRole("button", { name: /Inspecting page/ });
    expect(
      calls.map((call) => {
        const result = call.querySelector(".font-mono");
        return [
          visibleText(call).replace(result?.textContent ?? "", ""),
          result?.textContent,
        ];
      }),
    ).toEqual([
      ["Inspecting page ×2", "Evaluated JavaScript: returned 3 items"],
      ["Inspecting page", "Evaluated JavaScript: returned 5 items"],
    ]);
  });

  it("only the last unresolved call is expanded while two are in flight", () => {
    render(<NarrativeView turn={twoInFlightTurn()} />);
    expect(
      stepLines().map((line) => line.getAttribute("aria-expanded")),
    ).toEqual(["false", "false", "true"]);
  });

  it("a live step folds on click and opens again on the next", () => {
    render(<NarrativeView turn={groupedBrowseTurn()} />);
    const row = stepLines()[0]!;

    fireEvent.click(row);
    expect(screen.queryByRole("button", { name: /Opening page/ })).toBeNull();

    fireEvent.click(row);
    expect(screen.getByRole("button", { name: /Opening page/ })).toBeTruthy();
  });

  const REASON = "Checking whether the invoices sit behind a login";

  const narratedBrowseTurn = (
    narrationIteration: number,
  ): TurnNarrativeState => ({
    ...EMPTY_NARRATIVE,
    turnId: "turn-1",
    turnIndex: 0,
    designStarted: true,
    terminal: null,
    designActivity: [
      browseEntry(0, "navigate_browser", "Opened the sign-in page"),
      activityEntry({
        id: "n-1",
        kind: "narration",
        text: REASON,
        iteration: narrationIteration,
      }),
    ],
  });

  it("keeps every step's sentence where it was spoken as the turn grows", () => {
    const at = (second: number) => `2026-06-10T00:00:0${second}Z`;
    const said = (n: number, iteration: number) =>
      activityEntry({
        id: `n-${n}`,
        kind: "narration",
        text: `Reason ${n}`,
        iteration,
        timestamp: at(n * 2),
      });
    const activity = [
      {
        ...browseEntry(0, "navigate_browser", "Opened the page"),
        timestamp: at(1),
      },
      said(1, 0),
      activityEntry({
        id: "tr-w1",
        kind: "tool_result",
        toolName: "update_workflow",
        text: "Saved 1 block",
        success: true,
        iteration: 1,
        timestamp: at(3),
      }),
      said(2, 1),
    ];
    const turn = (entries: typeof activity): TurnNarrativeState => ({
      ...narratedBrowseTurn(0),
      designActivity: entries,
    });
    const reasons = () =>
      screen.queryAllByTestId("copilot-reason").map((node) => node.textContent);

    const { rerender } = render(<NarrativeView turn={turn(activity)} />);
    expect(reasons()).toEqual([
      expect.stringContaining("Reason 1"),
      expect.stringContaining("Reason 2"),
    ]);

    rerender(<NarrativeView turn={turn([...activity, said(3, 1)])} />);
    expect(reasons()).toEqual([
      expect.stringContaining("Reason 1"),
      expect.stringContaining("Reason 2"),
      expect.stringContaining("Reason 3"),
    ]);
  });

  it("unowned legacy narration stays standalone when the neighboring step folds", () => {
    render(<NarrativeView turn={narratedBrowseTurn(0)} />);

    expect(document.querySelectorAll("[data-activity-row-id]")).toHaveLength(2);
    const reason = screen.getByTestId("copilot-reason");
    expect(reason.textContent).toContain(REASON);
    expect(
      reason.compareDocumentPosition(stepLines()[0]!) &
        Node.DOCUMENT_POSITION_PRECEDING,
    ).toBeTruthy();

    fireEvent.click(stepLines()[0]!);
    expect(screen.getByTestId("copilot-reason").textContent).toContain(REASON);
  });

  it("a narration spoken after its step settled reads below it, leaving the step untouched", () => {
    render(<NarrativeView turn={narratedBrowseTurn(7)} />);

    expect(document.querySelectorAll("[data-activity-row-id]")).toHaveLength(2);
    const reason = screen.getByTestId("copilot-reason");
    expect(reason.textContent).toContain(REASON);
    expect(
      reason.compareDocumentPosition(stepLines()[0]!) &
        Node.DOCUMENT_POSITION_PRECEDING,
    ).toBeTruthy();
  });

  it("a finished run step opens to its block, and the block to its steps", () => {
    const turn = repairLoopTurn();
    turn.blocks = [
      runningBlock({
        state: "completed",
        endedAt: "2026-06-10T00:00:10Z",
        activity: [
          activityEntry({
            id: "tr-s1",
            kind: "tool_result",
            toolName: "click_element",
            text: "Submitted the form",
            success: true,
          }),
          activityEntry({
            id: "tr-s2",
            kind: "tool_result",
            toolName: "get_page_evidence",
            text: "Landed on the receipt page",
            success: true,
            iteration: 1,
          }),
        ],
      }),
    ];
    render(<NarrativeView turn={turn} />);
    openFold();
    fireEvent.click(stepLines()[1]!);
    expect(screen.queryByText("Submitted the form")).toBeNull();

    fireEvent.click(screen.getByTitle(/Highlight block_1/));
    expect(screen.getByText("Submitted the form")).toBeTruthy();
  });

  it("a run row with two blocks holds both cards behind one toggle", () => {
    const turn = repairLoopTurn();
    turn.blocks = [
      runningBlock({ state: "completed", endedAt: "2026-06-10T00:00:10Z" }),
      runningBlock({
        workflowRunBlockId: "wrb_2",
        label: "block_2",
        state: "completed",
        endedAt: "2026-06-10T00:00:12Z",
      }),
    ];
    render(<NarrativeView turn={turn} />);
    openFold();
    expect(screen.queryAllByRole("button", { name: /Block 1/ })).toHaveLength(
      0,
    );

    fireEvent.click(stepLines()[1]!);
    expect(screen.getAllByRole("button", { name: /Block 1/ })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: /Block 2/ })).toHaveLength(1);
  });

  const titledFailedRun = (): TurnNarrativeState => ({
    ...EMPTY_NARRATIVE,
    turnId: "turn-1",
    turnIndex: 0,
    designStarted: true,
    designEnded: true,
    terminal: "response",
    blocks: [
      runningBlock({
        workflowRunBlockId: "wrb_a",
        label: "sign_in",
        state: "completed",
        startedAt: null,
      }),
      runningBlock({
        workflowRunBlockId: "wrb_b",
        label: "download_latest_pdf",
        state: "failed",
        startedAt: null,
      }),
    ],
    designActivity: [
      activityEntry({
        id: "tr-look",
        kind: "tool_result",
        toolName: "navigate_browser",
        text: "Opened the portal",
        success: true,
      }),
      activityEntry({
        id: "tr-run",
        kind: "tool_result",
        toolName: "run_blocks_and_collect_debug",
        text: "download_latest_pdf timed out",
        success: false,
        iteration: 1,
      }),
      activityEntry({
        id: "n-run",
        kind: "narration",
        text: "Running both blocks against the live portal.",
        iteration: 1,
        activeLabel: "Testing the workflow end to end",
      }),
    ],
  });

  it("a failed block stays named on the collapsed step and on the folded turn", () => {
    render(<NarrativeView turn={titledFailedRun()} />);
    // The turn's fold says which block the newest test left failing.
    expect(
      visibleText(screen.getByRole("button", { name: /^Worked through/ })),
    ).toBe(
      "Worked through 2 steps · 1 failed test · Download Latest Pdf failed",
    );

    openFold();
    const runLine = stepLines()[1]!;
    expect(runLine.getAttribute("aria-expanded")).toBe("false");
    expect(visibleText(runLine)).toBe(
      "Tested the workflow · attempt failed · Download Latest Pdf failed",
    );
    // Attempt failures keep the neutral timeline palette on the line.
    expect(runLine.innerHTML).not.toContain("rose");
    expect(screen.queryByText("Test passed")).toBeNull();
  });

  it("a fixed turn says so and names nothing it no longer fails", () => {
    const turn = titledFailedRun();
    turn.turnFacts = {
      factsAvailable: true,
      authoredBlockCount: 2,
      matchingSourceBlockCount: 2,
      evaluationState: "demonstrated",
      runId: "wr_1",
      runCompleted: true,
      terminalCause: null,
      blocksRunThisTurn: 2,
      ranCleanOnCurrentSource: true,
    };
    render(<NarrativeView turn={turn} />);
    expect(
      visibleText(screen.getByRole("button", { name: /^Worked through/ })),
    ).toBe("Worked through 2 steps · 1 failed test, fixed");
  });

  it("an evidence-free drafted block never renders", () => {
    const turn = repairLoopTurn();
    turn.blocks = [
      runningBlock({
        workflowRunBlockId: "",
        label: "block_2",
        state: "drafted",
        startedAt: null,
      }),
    ];
    render(<NarrativeView turn={turn} />);
    openFold();
    expect(screen.queryByText("Block 2")).toBeNull();
    expect(stepLines()).toHaveLength(3);
  });

  it("filters evidence-free blocks from the fallback projection", () => {
    const turn: TurnNarrativeState = {
      ...EMPTY_NARRATIVE,
      turnId: "turn-fallback",
      blocks: [
        runningBlock({
          workflowRunBlockId: "",
          label: "snapshot_only",
          state: "drafted",
          startedAt: null,
        }),
        runningBlock({
          workflowRunBlockId: "wrb_observed",
          label: "observed_attempt",
          state: "completed",
        }),
      ],
    };

    render(<NarrativeView turn={turn} />);

    expect(screen.queryByText("Snapshot Only")).toBeNull();
    expect(screen.getByText("Observed Attempt")).toBeTruthy();
  });

  it("an empty drafted block cannot render or steal focus from live work", () => {
    const turn: TurnNarrativeState = {
      ...EMPTY_NARRATIVE,
      turnId: "turn-1",
      turnIndex: 0,
      designStarted: true,
      terminal: null,
      designActivity: [
        activityEntry({
          id: "tc-1",
          kind: "tool_call",
          toolName: "navigate_browser",
          displayLabel: "Searching the catalogue",
          timestamp: "2026-06-10T00:00:05Z",
        }),
        activityEntry({
          id: "n-1",
          kind: "narration",
          text: "Finding the first matching item.",
          iteration: 0,
          timestamp: "2026-06-10T00:00:06Z",
        }),
      ],
      blocks: [
        runningBlock({
          workflowRunBlockId: "",
          label: "add_first_result",
          state: "drafted",
          startedAt: null,
        }),
      ],
    };

    const { rerender } = render(<NarrativeView turn={turn} />);
    expect(stepLines()).toHaveLength(1);
    expect(
      screen.queryByRole("button", { name: /Add First Result/ }),
    ).toBeNull();

    rerender(
      <NarrativeView
        turn={{
          ...turn,
          designActivity: [
            activityEntry({
              id: "tr-1",
              kind: "tool_result",
              toolName: "navigate_browser",
              text: "Searched the catalogue",
              success: true,
              timestamp: "2026-06-10T00:00:06Z",
            }),
          ],
        }}
      />,
    );
    expect(stepLines()).toHaveLength(1);
    expect(
      screen.queryByRole("button", { name: /Add First Result/ }),
    ).toBeNull();
  });

  it("a hand-opened row stays open when a new row goes live, and resets next turn", () => {
    const { rerender } = render(<NarrativeView turn={twoInFlightTurn()} />);
    fireEvent.click(stepLines()[0]!);
    expect(stepLines()[0]!.getAttribute("aria-expanded")).toBe("true");

    // Liveness has to actually move, or this cannot tell a surviving click from
    // one the auto-rule never had a chance to stomp: resolve both open calls so
    // the old live row goes quiet, and open a new one that takes its place.
    const advanced = twoInFlightTurn();
    advanced.designActivity = [
      ...advanced.designActivity,
      activityEntry({
        id: "tr-3",
        kind: "tool_result",
        toolName: "update_workflow",
        displayLabel: "Saved blocks",
        success: true,
        iteration: 2,
      }),
      activityEntry({
        id: "tr-5",
        kind: "tool_result",
        toolName: "navigate_browser",
        displayLabel: "Opened page",
        success: true,
        iteration: 3,
      }),
      activityEntry({
        id: "tc-7",
        kind: "tool_call",
        toolName: "update_and_run_blocks",
        displayLabel: "Testing workflow",
        iteration: 5,
      }),
    ];
    rerender(<NarrativeView turn={advanced} />);

    // The hand-opened row survives the advance, and the row that just went
    // live is the newest one.
    const lines = stepLines();
    expect(lines[0]!.getAttribute("aria-expanded")).toBe("true");
    expect(visibleText(lines[lines.length - 1]!)).toBe("Testing the workflow");

    rerender(
      <NarrativeView turn={{ ...twoInFlightTurn(), turnId: "turn-2" }} />,
    );
    expect(stepLines()[0]!.getAttribute("aria-expanded")).toBe("false");
  });

  it("a still-calling run row is headed by its live line, not a finished block's verdict", () => {
    const turn: TurnNarrativeState = {
      ...EMPTY_NARRATIVE,
      turnId: "turn-1",
      turnIndex: 0,
      designStarted: true,
      terminal: null,
      blocks: [
        runningBlock({ state: "completed", startedAt: null, endedAt: null }),
      ],
      designActivity: [
        activityEntry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "update_and_run_blocks",
          text: "First run finished",
          success: true,
        }),
        activityEntry({
          id: "tr-2",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "Re-checked the page",
          success: true,
          iteration: 1,
        }),
        activityEntry({
          id: "tc-3",
          kind: "tool_call",
          toolName: "update_and_run_blocks",
          displayLabel: "Testing workflow",
          iteration: 2,
        }),
      ],
    };
    render(<NarrativeView turn={turn} />);

    const lines = stepLines();
    const header = lines[lines.length - 1]!;
    expect(header.getAttribute("aria-expanded")).toBe("true");
    expect(visibleText(header)).toBe("Testing the workflow");
  });

  it("a run whose every block passed says so on its line", () => {
    const finished = (id: string, label: string): BlockState =>
      runningBlock({
        workflowRunBlockId: id,
        label,
        state: "completed",
        outcome: "demonstrated",
        startedAt: null,
      });
    const turn: TurnNarrativeState = {
      ...EMPTY_NARRATIVE,
      turnId: "turn-1",
      turnIndex: 0,
      designStarted: true,
      designEnded: true,
      terminal: "response",
      blocks: [
        finished("wrb_a", "open_statement"),
        finished("wrb_b", "read_amount"),
      ],
      designActivity: [
        activityEntry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "update_and_run_blocks",
          text: "Ran the first draft",
          success: false,
        }),
        activityEntry({
          id: "tr-2",
          kind: "tool_result",
          toolName: "get_page_evidence",
          text: "Read the form state",
          success: true,
        }),
        activityEntry({
          id: "tr-3",
          kind: "tool_result",
          toolName: "update_and_run_blocks",
          text: "Reached the confirmation page",
          success: true,
        }),
      ],
    };
    render(<NarrativeView turn={turn} />);
    openFold();

    expect(stepLines().map(visibleText)).toEqual([
      "Tested the workflow · attempt failed",
      "1 browser action",
      "Tested the workflow · 2 of 2 passed",
    ]);
  });

  it.each([["not_evaluated" as const], [undefined]])(
    "does not call a run passed when its block's verdict is %s",
    (outcome) => {
      const turn: TurnNarrativeState = {
        ...EMPTY_NARRATIVE,
        turnId: "turn-1",
        turnIndex: 0,
        designStarted: true,
        designEnded: true,
        terminal: "response",
        blocks: [
          runningBlock({
            workflowRunBlockId: "wrb_a",
            label: "open_statement",
            state: "completed",
            startedAt: null,
            outcome,
          }),
        ],
        designActivity: [
          activityEntry({
            id: "tr-1",
            kind: "tool_result",
            toolName: "update_and_run_blocks",
            text: "Ran the draft",
            success: true,
          }),
        ],
      };
      render(<NarrativeView turn={turn} />);

      const lines = stepLines().map(visibleText);
      expect(lines).toEqual(["Tested the workflow"]);
    },
  );

  it("a block whose run row was evicted still renders inside a row at Done", () => {
    const turn: TurnNarrativeState = {
      ...EMPTY_NARRATIVE,
      turnId: "turn-1",
      turnIndex: 0,
      designStarted: true,
      designEnded: true,
      terminal: "response",
      blocks: [
        runningBlock({ state: "completed", startedAt: null, endedAt: null }),
      ],
      designActivity: [
        activityEntry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "update_workflow",
          text: "Saved 2 blocks",
          success: true,
        }),
      ],
    };
    render(<NarrativeView turn={turn} />);
    openFold();

    expect(document.querySelectorAll("[data-activity-row-id]")).toHaveLength(2);
    expect(screen.getByTitle(/Highlight block_1/)).toBeTruthy();
    expect(
      stepLines().map((line) => line.getAttribute("aria-expanded")),
    ).toEqual(["false"]);
  });

  const failedRunTurn = (): TurnNarrativeState => ({
    ...EMPTY_NARRATIVE,
    turnId: "turn-1",
    turnIndex: 0,
    designStarted: true,
    designEnded: true,
    terminal: "response",
    narrativeSummary: "Built it.",
    draft: { blockCount: 1, blockLabels: ["download_block"], summary: null },
    blocks: [
      runningBlock({
        workflowRunBlockId: "wrb_dl",
        label: "download_block",
        state: "failed",
        endedAt: "2026-06-10T00:00:20Z",
      }),
    ],
    designActivity: [
      activityEntry({
        id: "tr-1",
        kind: "tool_result",
        toolName: "update_and_run_blocks",
        displayLabel: "Ran it",
        success: false,
      }),
    ],
  });

  it("clicking the live row folds it instead of pinning it open", () => {
    render(<NarrativeView turn={twoInFlightTurn()} />);
    const live = stepLines()[2]!;

    fireEvent.click(live);
    expect(live.getAttribute("aria-expanded")).toBe("false");
  });

  it("a one-step finished turn is not folded, and its block opens to its steps", () => {
    const turn = failedRunTurn();
    turn.blocks = [
      runningBlock({
        workflowRunBlockId: "wrb_dl",
        label: "download_block",
        state: "completed",
        endedAt: "2026-06-10T00:00:20Z",
        activity: [
          activityEntry({
            id: "tr-sub",
            kind: "tool_result",
            toolName: "click_element",
            text: "Clicked the download link",
            success: true,
          }),
        ],
      }),
    ];
    render(<NarrativeView turn={turn} />);
    expect(
      screen.queryByRole("button", { name: /^Worked through/ }),
    ).toBeNull();
    expect(screen.queryByText("Clicked the download link")).toBeNull();

    fireEvent.click(stepLines()[0]!);
    fireEvent.click(screen.getByTitle(/Highlight download_block/));
    expect(screen.getByText("Clicked the download link")).toBeTruthy();
  });

  it("the live run row is already open when its block lands", () => {
    const inFlight: TurnNarrativeState = {
      ...EMPTY_NARRATIVE,
      turnId: "turn-1",
      turnIndex: 0,
      designStarted: true,
      terminal: null,
      designActivity: [
        activityEntry({
          id: "tc-2",
          kind: "tool_call",
          toolName: "update_and_run_blocks",
          displayLabel: "Testing workflow",
          iteration: 1,
        }),
      ],
    };
    const { rerender } = render(<NarrativeView turn={inFlight} />);
    expect(stepLines()[0]!.getAttribute("aria-expanded")).toBe("true");

    // Once the block dispatches it anchors to that same row, and the row must
    // already be open — no click — or the running block card renders folded.
    const withBlock: TurnNarrativeState = {
      ...inFlight,
      blocks: [
        runningBlock({ workflowRunBlockId: "wrb_1", label: "download_block" }),
      ],
    };
    rerender(<NarrativeView turn={withBlock} />);
    expect(stepLines()[0]!.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTitle(/Highlight download_block/)).toBeTruthy();
  });

  it("a live row with one step renders that step once", () => {
    const turn: TurnNarrativeState = {
      ...EMPTY_NARRATIVE,
      turnId: "turn-1",
      turnIndex: 0,
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
    };
    render(<NarrativeView turn={turn} />);
    expect(stepLines().map(visibleText)).toEqual(["1 browser action"]);
    expect(screen.queryAllByText(/Opening page/)).toHaveLength(1);
  });

  it("names a failed row by its calls and keeps the exact error in its detail", () => {
    render(
      <NarrativeView
        turn={{
          ...EMPTY_NARRATIVE,
          turnId: "turn-1",
          turnIndex: 0,
          designStarted: true,
          terminal: null,
          designEnded: true,
          designActivity: [
            activityEntry({
              id: "tr-f1",
              kind: "tool_result",
              toolName: "update_and_run_blocks",
              text: "The submit button stayed disabled after filling the form",
              success: false,
              iteration: 0,
            }),
            activityEntry({
              id: "n-f1",
              kind: "narration",
              text: REASON,
              iteration: 0,
              activeLabel: "Running it",
            }),
            activityEntry({
              id: "tr-next",
              kind: "tool_result",
              toolName: "navigate_browser",
              text: "Opened a fresh browser",
              success: true,
              iteration: 1,
            }),
          ],
        }}
      />,
    );

    // The outcome label is a stale prediction on failure. The collapsed row
    // keeps the trustworthy intent, while the exact server error remains
    // available one level deeper instead of becoming the primary headline.
    const failedLine = stepLines()[0]!;
    expect(visibleText(failedLine)).toBe(
      "Tested the workflow · attempt failed",
    );
    expect(failedLine.innerHTML).not.toContain("rose");
    expect(
      screen.queryByText(
        "The submit button stayed disabled after filling the form",
      ),
    ).toBeNull();
    expect(screen.queryByText("Ran it - everything passed")).toBeNull();

    fireEvent.click(failedLine);
    expect(
      screen.getByText(
        "The submit button stayed disabled after filling the form",
      ),
    ).toBeTruthy();
  });

  it("keeps a focused retry failure neutral", () => {
    render(
      <NarrativeView
        turn={{
          ...EMPTY_NARRATIVE,
          turnId: "turn-1",
          turnIndex: 0,
          designStarted: true,
          terminal: null,
          designActivity: [
            activityEntry({
              id: "tr-f1",
              kind: "tool_result",
              toolName: "get_page_evidence",
              text: "The browser target crashed",
              displayLabel: "Restoring access to the certification results",
              success: false,
              iteration: 1,
              attempts: 2,
            }),
          ],
        }}
      />,
    );

    const line = stepLines()[0]!;
    expect(visibleText(line)).toBe(
      "1 browser action · attempt failed · ↻ 2 attempts",
    );
    expect(line.innerHTML).not.toContain("rose");
  });
});

const FIRST_PATCH =
  "@@ -1,2 +1,2 @@\n-await page.click('#a')\n+await page.click('#b')";
const SECOND_PATCH =
  "@@ -1,2 +1,3 @@\n await page.goto(URL)\n+await page.wait_for_timeout(500)";

const twoWriteTurn = (
  overrides: Partial<TurnNarrativeState> = {},
): TurnNarrativeState => ({
  ...EMPTY_NARRATIVE,
  turnId: "turn-1",
  turnIndex: 0,
  designStarted: true,
  terminal: null,
  draft: { blockCount: 1, blockLabels: ["download_step"], summary: null },
  designActivity: [
    activityEntry({
      id: "tr-1",
      kind: "tool_result",
      toolName: "update_and_run_blocks",
      text: "Saved and ran the download step",
      success: true,
      codeDiffs: [
        {
          label: "download_step",
          added: 4,
          removed: 2,
          patch: FIRST_PATCH,
        },
      ],
    }),
    activityEntry({
      id: "tr-2",
      kind: "tool_result",
      toolName: "edit_block_and_run",
      text: "Repaired the download step",
      success: true,
      iteration: 1,
      codeDiffs: [
        {
          label: "download_step_v2",
          added: 1,
          removed: 0,
          patch: SECOND_PATCH,
        },
      ],
    }),
  ],
  ...overrides,
});

describe("NarrativeView — code write diffs", () => {
  it("shows an active write as a muted code peek that expands on click", () => {
    render(
      <NarrativeView
        turn={{
          ...EMPTY_NARRATIVE,
          turnId: "turn-1",
          turnIndex: 0,
          designStarted: true,
          terminal: null,
          designActivity: [
            activityEntry({
              id: "tr-write",
              kind: "tool_result",
              toolName: "add_block",
              text: "Added the cart block",
              success: true,
              iteration: 2,
              codeDiffs: [
                {
                  label: "add_to_cart",
                  added: 1,
                  removed: 0,
                  patch: "+await page.click('button.add-to-cart')",
                },
              ],
            }),
            activityEntry({
              id: "tc-run",
              kind: "tool_call",
              toolName: "run_blocks_and_collect_debug",
              displayLabel: "Testing the cart block",
              iteration: 3,
            }),
          ],
        }}
      />,
    );

    expect(screen.getByText(/Testing the cart block/)).toBeTruthy();
    const peek = screen.getByRole("button", {
      name: "Expand code changes for add_to_cart",
    });
    expect(peek.getAttribute("data-code-diff-peek")).toBe("true");
    expect(peek.getAttribute("aria-expanded")).toBe("false");
    expect(peek.className).toContain("max-h-");
    expect(
      screen.getByText("+await page.click('button.add-to-cart')").className,
    ).toContain("!text-muted-foreground");
    expect(peek.className).not.toContain("mask-image");
    expect(screen.queryByRole("button", { name: "view diff" })).toBeNull();

    fireEvent.click(peek);
    const collapse = screen.getByRole("button", { name: "hide diff" });
    expect(collapse).toBeTruthy();
    expect(document.activeElement).toBe(collapse);
    expect(
      screen.getByText("+await page.click('button.add-to-cart')").className,
    ).not.toContain("opacity-50");
  });

  it("keeps an active repair's code in its block's card before its tool result arrives", () => {
    render(
      <NarrativeView
        turn={{
          ...EMPTY_NARRATIVE,
          turnId: "turn-1",
          turnIndex: 0,
          designStarted: true,
          terminal: null,
          draft: { blockCount: 1, blockLabels: ["add_to_cart"], summary: null },
          blocks: [
            runningBlock({
              label: "add_to_cart",
              activity: [
                activityEntry({
                  id: "tc-repair",
                  kind: "tool_call",
                  toolName: "edit_block_and_run",
                  displayLabel: "Repairing and testing the cart block",
                  codeDiffs: [
                    {
                      label: "add_to_cart",
                      added: 1,
                      removed: 1,
                      patch: "-old line\n+new line",
                    },
                  ],
                }),
              ],
            }),
          ],
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Code change/ }));
    expect(screen.getByText("+new line")).toBeTruthy();
  });

  it("pins the containing row when the active code peek is expanded", () => {
    const write = activityEntry({
      id: "tr-write",
      kind: "tool_result",
      toolName: "add_block",
      text: "Added the cart block",
      success: true,
      iteration: 0,
      codeDiffs: [
        {
          label: "add_to_cart",
          added: 1,
          removed: 0,
          patch: "+await page.click('button.add-to-cart')",
        },
      ],
    });
    const initial = twoWriteTurn({ designActivity: [write] });
    const { rerender } = render(<NarrativeView turn={initial} />);

    fireEvent.click(
      screen.getByRole("button", {
        name: "Expand code changes for add_to_cart",
      }),
    );
    rerender(
      <NarrativeView
        turn={twoWriteTurn({
          designActivity: [
            write,
            activityEntry({
              id: "tr-browse",
              kind: "tool_result",
              toolName: "navigate_browser",
              text: "Opened the cart",
              success: true,
              iteration: 1,
            }),
          ],
        })}
      />,
    );

    expect(screen.getByRole("button", { name: "hide diff" })).toBeTruthy();
    expect(
      screen.getByText("+await page.click('button.add-to-cart')"),
    ).toBeTruthy();
  });

  it("clears live row and diff overrides when the turn becomes terminal", async () => {
    const live = twoWriteTurn();
    const { container, rerender } = render(<NarrativeView turn={live} />);
    fireEvent.click(
      screen.getByRole("button", {
        name: "Expand code changes for download_step_v2",
      }),
    );
    expect(screen.getByRole("button", { name: "hide diff" })).toBeTruthy();

    rerender(<NarrativeView turn={{ ...live, terminal: "response" }} />);

    expect(container.querySelector('[aria-expanded="true"]')).toBeNull();
    expect(screen.queryByRole("button", { name: "hide diff" })).toBeNull();
    expect(screen.queryByText("+await page.wait_for_timeout(500)")).toBeNull();
    // The row the reader was on folds with the turn, so focus lands on the fold.
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("button", { name: /^Worked through/ }),
      ),
    );
  });

  it("restores a keyboard-focused activity row when a live turn becomes terminal", async () => {
    const live = twoWriteTurn();
    const { rerender } = render(<NarrativeView turn={live} />);
    const peek = screen.getByRole("button", {
      name: "Expand code changes for download_step_v2",
    });
    peek.focus();
    expect(document.activeElement).toBe(peek);

    rerender(<NarrativeView turn={{ ...live, terminal: "response" }} />);

    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("button", { name: /^Worked through/ }),
      ),
    );
  });

  it("shows an explanation control when the active patch was dropped", () => {
    render(
      <NarrativeView
        turn={twoWriteTurn({
          designActivity: [
            activityEntry({
              id: "tr-large",
              kind: "tool_result",
              toolName: "add_block",
              text: "Added a large block",
              success: true,
              codeDiffs: [
                {
                  label: "large_block",
                  added: 1200,
                  removed: 0,
                  patchDropped: true,
                },
              ],
            }),
          ],
        })}
      />,
    );

    expect(
      screen.queryByRole("button", {
        name: "Expand code changes for large_block",
      }),
    ).toBeNull();
    expect(
      (
        screen.getByRole("button", {
          name: "view diff",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    expect(
      screen.getByTitle(
        "The diff was too large to keep, so only its line counts were saved.",
      ),
    ).toBeTruthy();
  });

  it("keeps diff counts on a combined write and solo-block run row", () => {
    render(
      <NarrativeView
        turn={{
          ...twoWriteTurn({
            designActivity: [
              activityEntry({
                id: "tr-write",
                kind: "tool_result",
                toolName: "add_block",
                text: "Added the cart block",
                success: true,
                codeDiffs: [
                  {
                    label: "add_to_cart",
                    added: 4,
                    removed: 2,
                    patch: FIRST_PATCH,
                  },
                ],
              }),
              activityEntry({
                id: "tr-run",
                kind: "tool_result",
                toolName: "run_blocks_and_collect_debug",
                text: "Tested the cart block",
                success: true,
                iteration: 1,
              }),
            ],
          }),
          blocks: [
            runningBlock({
              state: "completed",
              outcome: "demonstrated",
              label: "add_to_cart",
              endedAt: "2026-06-10T00:00:08Z",
            }),
          ],
        }}
      />,
    );

    expect(screen.getAllByText("+4").length).toBeGreaterThan(0);
    expect(screen.getAllByText("−2").length).toBeGreaterThan(0);
  });

  it("a streaming turn auto-opens only the newest write's patch", () => {
    render(<NarrativeView turn={twoWriteTurn()} />);

    expect(screen.queryByText("-await page.click('#a')")).toBeNull();
    expect(screen.getByText(/await page\.goto\(URL\)/)).toBeTruthy();
    expect(screen.queryByText("+await page.wait_for_timeout(500)")).toBeNull();
    expect(screen.queryByRole("button", { name: "view diff" })).toBeNull();
    expect(
      screen.getAllByRole("button", { name: /Expand code changes/ }),
    ).toHaveLength(1);
  });

  it("limits the active peek to its first lines but reveals the full patch", () => {
    const patch = [
      "@@ -1,7 +1,7 @@",
      "-line one",
      "+line one revised",
      " line two",
      " line three",
      " line four",
      "+line five",
    ].join("\n");
    render(
      <NarrativeView
        turn={twoWriteTurn({
          designActivity: [
            activityEntry({
              id: "tr-long",
              kind: "tool_result",
              toolName: "edit_block_and_run",
              text: "Updated the search block",
              success: true,
              codeDiffs: [
                {
                  label: "search",
                  added: 2,
                  removed: 1,
                  patch,
                },
              ],
            }),
          ],
        })}
      />,
    );

    expect(screen.queryByText("+line five")).toBeNull();
    fireEvent.click(
      screen.getByRole("button", {
        name: "Expand code changes for search",
      }),
    );
    expect(screen.getByText("+line five")).toBeTruthy();
  });

  it("does not show the faded peek when the user manually re-opens a row", () => {
    const { container } = render(<NarrativeView turn={twoWriteTurn()} />);
    const currentRow = stepLines()[1]!;

    expect(container.querySelector("[data-code-diff-peek]")).toBeTruthy();
    fireEvent.click(currentRow);
    fireEvent.click(currentRow);

    expect(container.querySelector("[data-code-diff-peek]")).toBeNull();
    expect(screen.getByRole("button", { name: "view diff" })).toBeTruthy();
  });

  it("a historical write reveals its patch only after the row and diff are opened", () => {
    render(<NarrativeView turn={twoWriteTurn()} />);

    fireEvent.click(stepLines()[0]!);

    expect(screen.queryByText("-await page.click('#a')")).toBeNull();
    expect(screen.getAllByRole("button", { name: "view diff" }).length).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "view diff" }));
    expect(screen.getByText("-await page.click('#a')")).toBeTruthy();
    expect(screen.getByText(/await page\.goto\(URL\)/)).toBeTruthy();
  });

  it("keeps collapsed diff counts quiet while preserving patch syntax colors", () => {
    render(<NarrativeView turn={twoWriteTurn({ terminal: "response" })} />);
    openFold();

    const addedCount = screen.getByText("+4");
    const removedCount = screen.getByText("−2");
    expect(addedCount.className).not.toContain("emerald");
    expect(removedCount.className).not.toContain("rose");

    fireEvent.click(stepLines()[0]!);
    fireEvent.click(screen.getAllByRole("button", { name: "view diff" })[0]!);
    expect(screen.getByText("-await page.click('#a')").className).toContain(
      "rose",
    );
    expect(screen.getByText("+await page.click('#b')").className).toContain(
      "emerald",
    );
  });

  it("at Done every write row is collapsed and view diff re-opens one", () => {
    const { container } = render(
      <NarrativeView turn={twoWriteTurn({ terminal: "response" })} />,
    );
    openFold();

    expect(container.querySelector("[data-code-diff-peek]")).toBeNull();
    expect(screen.queryByText("+await page.wait_for_timeout(500)")).toBeNull();
    // Counts stay on the collapsed row line; the patch is behind the expander.
    expect(screen.getByText("+4")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "view diff" })).toBeNull();

    fireEvent.click(stepLines()[0]!);
    fireEvent.click(screen.getAllByRole("button", { name: "view diff" })[0]!);
    expect(screen.getByText("-await page.click('#a')")).toBeTruthy();
  });

  it("a payload without the new keys renders its steps without counts", () => {
    const turn = twoWriteTurn({ terminal: "response" });
    const legacy = {
      ...turn,
      designActivity: turn.designActivity.map(({ ...entry }) => {
        delete entry.codeDiffs;
        return entry;
      }),
    };
    render(<NarrativeView turn={legacy} />);
    openFold();

    expect(stepLines().map(visibleText)).toEqual([
      "Tested the workflow",
      "Tested the workflow",
    ]);
    expect(screen.queryByText("+4")).toBeNull();
    expect(screen.queryByText(/view diff/)).toBeNull();
    expect(screen.queryByText("-await page.click('#a')")).toBeNull();
  });

  it("a dropped patch keeps its counts and disables view diff", () => {
    const turn = twoWriteTurn({ terminal: "response" });
    const dropped = {
      ...turn,
      designActivity: [
        {
          ...turn.designActivity[0]!,
          codeDiffs: [
            {
              label: "download_step",
              added: 4,
              removed: 2,
              patchDropped: true,
            },
          ],
        },
      ],
    };
    render(<NarrativeView turn={dropped} />);

    expect(screen.getByText("+4")).toBeTruthy();
    expect(screen.getByText("−2")).toBeTruthy();

    fireEvent.click(stepLines()[0]!);
    const toggle = screen.getByRole("button", { name: "view diff" });
    expect(toggle.hasAttribute("disabled")).toBe(true);
    expect(screen.queryByText("-await page.click('#a')")).toBeNull();
  });

  it("hydrating a persisted payload keeps the same counts and patch", () => {
    const hydrated = hydrateNarrativeFromPayload({
      turnId: "turn-1",
      turnIndex: 0,
      designStarted: true,
      terminal: "response",
      draft: { blockCount: 1, blockLabels: ["download_step"], summary: null },
      designActivity: [
        {
          id: "tr-1",
          kind: "tool_result",
          text: "Saved and ran the download step",
          iteration: 0,
          toolName: "update_and_run_blocks",
          success: true,
          codeDiffs: [
            {
              label: "download_step",
              added: 4,
              removed: 2,
              patch: FIRST_PATCH,
            },
          ],
        },
      ],
    });
    expect(hydrated).not.toBeNull();
    render(<NarrativeView turn={hydrated!} />);

    expect(screen.getByText("+4")).toBeTruthy();
    expect(screen.getByText("−2")).toBeTruthy();
    fireEvent.click(stepLines()[0]!);
    fireEvent.click(screen.getByRole("button", { name: "view diff" }));
    expect(screen.getByText("-await page.click('#a')")).toBeTruthy();
  });
});
