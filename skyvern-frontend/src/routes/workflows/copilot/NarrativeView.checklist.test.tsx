// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
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

const KIND_GLYPH_PATTERN = /^(◎|⟨⟩|▷)$/;

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
  it("the rollup renders the flat activity rows and drops the phase rail", () => {
    render(<NarrativeView turn={repairLoopTurn()} />);
    expect(screen.queryByText("Explore site")).toBeNull();
    expect(screen.queryByText("Draft code")).toBeNull();
    expect(screen.queryByText("Test-run")).toBeNull();
    expect(screen.getByText("Opened the sign-in page")).toBeTruthy();
    expect(screen.getByText("Saved 2 blocks")).toBeTruthy();
  });

  it("moves focus between the terminal rollup and detail disclosures", async () => {
    render(<NarrativeView turn={repairLoopTurn()} />);
    const rollup = screen.getByRole("button", {
      name: /Built the workflow/,
    });
    expect(rollup.getAttribute("aria-expanded")).toBe("false");
    expect(rollup.hasAttribute("aria-controls")).toBe(false);
    fireEvent.click(rollup);

    const collapse = screen.getByRole("button", { name: "Collapse turn" });
    expect(collapse.getAttribute("aria-expanded")).toBe("true");
    expect(
      document.getElementById(collapse.getAttribute("aria-controls")!),
    ).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(collapse));

    fireEvent.click(collapse);
    const restored = screen.getByRole("button", {
      name: /Built the workflow/,
    });
    await waitFor(() => expect(document.activeElement).toBe(restored));
  });

  it("a failed run keeps the server's reason instead of a generic tool label", () => {
    render(<NarrativeView turn={repairLoopTurn()} />);
    expect(
      screen.queryByText(
        "The submit button stayed disabled after filling the form",
      ),
    ).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Block 1/ }));
    expect(
      screen.getByText(
        "The submit button stayed disabled after filling the form",
      ),
    ).toBeTruthy();
  });

  it("an in-flight turn renders the same flat rows in happened-order", () => {
    render(<NarrativeView turn={{ ...repairLoopTurn(), terminal: null }} />);
    expect(screen.queryByText("Explore site")).toBeNull();
    const rows = screen.getAllByText(/Opened the sign-in page|Saved 2 blocks/);
    expect(rows.map((r) => r.textContent)).toEqual([
      "Opened the sign-in page",
      "Saved 2 blocks",
    ]);
  });

  it("the rollup gutter reads the row kinds in happened-order", () => {
    render(<NarrativeView turn={repairLoopTurn()} />);
    const gutter = screen.getAllByText(KIND_GLYPH_PATTERN);
    expect(gutter.map((g) => g.textContent)).toEqual(["◎", "▷", "⟨⟩"]);
  });

  it("the in-flight detail gutter reads the same row kinds", () => {
    render(<NarrativeView turn={{ ...repairLoopTurn(), terminal: null }} />);
    const gutter = screen.getAllByText(KIND_GLYPH_PATTERN);
    expect(gutter.map((g) => g.textContent)).toEqual(["◎", "▷", "⟨⟩"]);
  });

  it("the block card is the run row itself, ahead of the re-authoring row that follows it", () => {
    render(<NarrativeView turn={repairLoopTurn()} />);
    const reauthoringRow = screen.getByText("Saved 2 blocks");
    const cards = screen.getAllByRole("button", { name: /Block 1/ });
    expect(cards.length).toBeGreaterThan(0);
    for (const card of cards) {
      expect(
        reauthoringRow.compareDocumentPosition(card) &
          Node.DOCUMENT_POSITION_PRECEDING,
      ).toBeTruthy();
    }
  });

  it("a block row wears the same row treatment as a step row", () => {
    render(<NarrativeView turn={repairLoopTurn()} />);
    const blockRow = screen
      .getAllByRole("button", { name: /Block 1/ })
      .find((b) => b.className.includes("grid-cols-"));

    // The block used to lead with a 24px status puck and a bold label while
    // the steps beside it led with a 16px glyph, so the column never lined up.
    expect(blockRow).toBeTruthy();
    expect(blockRow!.querySelector(".rounded-full")).toBeNull();
    expect(blockRow!.className).toContain("grid-cols-[18px_1fr_auto]");

    // State is the inline mark and the elapsed column now, not a trailing
    // "· done · code" the canvas rows never carry. The state still reaches a
    // screen reader as a word, so read what is on screen rather than the
    // sr-only text alongside it.
    const onScreen = blockRow!.cloneNode(true) as HTMLElement;
    onScreen.querySelectorAll(".sr-only").forEach((n) => n.remove());
    expect(onScreen.textContent).not.toContain("code");
    expect(onScreen.textContent).not.toContain("done");
  });

  it("a live row's clock reads wall time, not the span of its entries", () => {
    const startedAt = new Date(Date.now() - 90_000).toISOString();
    const turn = repairLoopTurn();
    turn.terminal = null;
    turn.blocks = [];
    turn.designActivity = [
      activityEntry({
        id: "tc-live",
        kind: "tool_call",
        toolName: "navigate_browser",
        text: "Opening the page",
        iteration: 0,
        timestamp: startedAt,
      }),
    ];
    render(<NarrativeView turn={turn} />);

    // One entry means first stamp === last stamp, so the recorded span is zero
    // and the column sat at 0:00 for as long as the step took.
    expect(screen.getByText("1:30")).toBeTruthy();
    expect(screen.queryByText("0:00")).toBeNull();
  });

  it("a run row still calling carries no success mark", () => {
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

    // A mark reports an outcome. The row is mid-call, so it has none yet —
    // before this, "not failed" was rendered as "succeeded".
    const row = screen
      .getAllByText(/calling…/)
      .map((n) => n.closest("[class*='grid-cols-']"))
      .find(Boolean);
    expect(row).toBeTruthy();
    expect(row!.textContent).toContain("calling…");
    expect(row!.textContent).not.toContain("✓");
  });

  it("the kind reaches a screen reader as a word, not only as the glyph", () => {
    render(<NarrativeView turn={repairLoopTurn()} />);
    // The glyph is aria-hidden, so without this the kind is invisible to a
    // screen reader — the phase rail pairs its puck with an sr-only word too.
    expect(screen.getAllByText(/Looked at the page ·/).length).toBeGreaterThan(
      0,
    );
    expect(screen.getAllByText(/Ran it ·/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Wrote code ·/).length).toBeGreaterThan(0);
  });

  it("a block-authoring row carries the authoring glyph", () => {
    const turn = repairLoopTurn();
    turn.designActivity = [
      activityEntry({
        id: "tr-9",
        kind: "tool_result",
        toolName: "edit_block",
        text: "Reworked the login block",
        success: true,
      }),
    ];
    render(<NarrativeView turn={turn} />);
    const gutter = screen.getAllByText(KIND_GLYPH_PATTERN);
    expect(gutter.map((g) => g.textContent)).toEqual(["⟨⟩", "▷"]);
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
      browseEntry(1, "get_page_evidence", "Read the form state"),
      browseEntry(2, "click_element", "Found the invoice list"),
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

  it("three consecutive browse steps fold into one row carrying the step count", () => {
    render(<NarrativeView turn={groupedBrowseTurn()} />);
    const gutter = screen.getAllByText(KIND_GLYPH_PATTERN);
    expect(gutter.map((g) => g.textContent)).toEqual(["◎"]);
    expect(screen.getByText("Found the invoice list")).toBeTruthy();

    // The turn has not ended, so the newest row is the one being worked on and
    // stays open through the gap between calls.
    expect(screen.getByText("Opened the sign-in page")).toBeTruthy();
    expect(screen.getByText("Read the form state")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { expanded: true }));
    expect(screen.getByText(/3 steps/)).toBeTruthy();
    expect(screen.queryByText("Opened the sign-in page")).toBeNull();
  });

  it("only the last unresolved call is expanded while two are in flight", () => {
    render(<NarrativeView turn={twoInFlightTurn()} />);

    expect(screen.getAllByRole("button", { expanded: true })).toHaveLength(1);
    expect(screen.getByText("Checked the cart")).toBeTruthy();
    expect(screen.queryByText("Opened the sign-in page")).toBeNull();
    expect(screen.getByRole("button", { expanded: false })).toBeTruthy();
  });

  it("a finished browse row re-opens on click and folds again on the next", () => {
    render(<NarrativeView turn={groupedBrowseTurn()} />);
    const row = screen.getByRole("button", { expanded: true });

    fireEvent.click(row);
    expect(screen.queryByText("Opened the sign-in page")).toBeNull();

    fireEvent.click(row);
    expect(screen.getByText("Opened the sign-in page")).toBeTruthy();
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

  it.each([0, 7])(
    "narration tagged iteration=%s renders inside the browse step, never as its own row",
    (narrationIteration) => {
      render(<NarrativeView turn={narratedBrowseTurn(narrationIteration)} />);

      expect(
        screen.getAllByText(KIND_GLYPH_PATTERN).map((g) => g.textContent),
      ).toEqual(["◎"]);
      // Inside the step, not beside it: one glyph means one row.
      expect(screen.getByText(REASON)).toBeTruthy();

      fireEvent.click(screen.getByRole("button", { expanded: true }));
      expect(screen.queryByText(REASON)).toBeNull();
    },
  );

  it("a finished run row re-opens to the block's step list", () => {
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
    expect(screen.queryByText("Submitted the form")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Block 1/ }));
    expect(screen.getByText("Submitted the form")).toBeTruthy();
  });

  it("a run row with no block renders a plain line and no block card", () => {
    const turn = repairLoopTurn();
    turn.blocks = [];
    render(<NarrativeView turn={turn} />);
    expect(screen.queryByText("Block 1")).toBeNull();
    expect(
      screen.queryByText(
        "The submit button stayed disabled after filling the form",
      ),
    ).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Testing workflow/ }));
    expect(
      screen.getByText(
        "The submit button stayed disabled after filling the form",
      ),
    ).toBeTruthy();
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
    expect(screen.queryAllByRole("button", { name: /Block 1/ })).toHaveLength(
      0,
    );
    expect(screen.queryAllByRole("button", { name: /Block 2/ })).toHaveLength(
      0,
    );

    fireEvent.click(screen.getByRole("button", { name: /Testing workflow/ }));
    expect(
      screen.getAllByRole("button", { name: /Block 1/ }).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByRole("button", { name: /Block 2/ }).length,
    ).toBeGreaterThan(0);
  });

  it("a drafted block never renders under a run row", () => {
    const turn = repairLoopTurn();
    turn.blocks = [
      runningBlock({
        workflowRunBlockId: "",
        label: "block_2",
        state: "drafted",
      }),
    ];
    render(<NarrativeView turn={turn} />);
    const card = screen.getByText("Block 2");
    const runRow = screen.getByText("Testing workflow");
    expect(
      runRow.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("an empty drafted block cannot hide or open ahead of live work", () => {
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

    const { container, rerender } = render(<NarrativeView turn={turn} />);
    const liveRow = screen.getByRole("button", {
      name: /Searching the catalogue/,
      expanded: true,
    });
    const draftRow = screen.getByRole("button", {
      name: /Add First Result/,
    });

    expect(liveRow).toBeTruthy();
    expect(draftRow.getAttribute("aria-expanded")).toBeNull();
    fireEvent.click(draftRow);
    expect(draftRow.getAttribute("aria-expanded")).toBeNull();
    expect(container.querySelectorAll(".border-l")).toHaveLength(1);

    // The newest row still owns focus during a real between-call gap, but a
    // contentless draft remains a plain row rather than an empty disclosure.
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
    expect(
      screen
        .getByRole("button", { name: /Add First Result/ })
        .getAttribute("aria-expanded"),
    ).toBeNull();
    expect(container.querySelectorAll(".border-l")).toHaveLength(0);
  });

  it("a hand-opened row stays open when a new row goes live, and resets next turn", () => {
    const { rerender } = render(<NarrativeView turn={twoInFlightTurn()} />);
    fireEvent.click(screen.getByRole("button", { expanded: false }));
    expect(screen.getByText("Opened the sign-in page")).toBeTruthy();

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

    // The hand-opened row survives the advance...
    expect(screen.getByText("Opened the sign-in page")).toBeTruthy();
    // ...and the row that just went live is the one the rule opened.
    expect(screen.getByText(/Testing workflow/)).toBeTruthy();

    rerender(
      <NarrativeView turn={{ ...twoInFlightTurn(), turnId: "turn-2" }} />,
    );
    expect(screen.queryByText("Opened the sign-in page")).toBeNull();
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

    const header = screen.getByRole("button", { expanded: true });
    expect(header.textContent).toContain("Testing workflow");
    expect(header.textContent).toContain("calling…");
    expect(header.textContent).not.toContain("done");
  });

  it("the step count folds away while the row is expanded", () => {
    render(<NarrativeView turn={groupedBrowseTurn()} />);
    expect(screen.queryByText(/3 steps/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { expanded: true }));
    expect(screen.getByText(/3 steps/)).toBeTruthy();
  });

  it("a collapsed run row says how many blocks are folded inside it", () => {
    const finished = (id: string, label: string): BlockState =>
      runningBlock({
        workflowRunBlockId: id,
        label,
        state: "completed",
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

    const rowHolding = screen
      .getAllByRole("button")
      .find((b) => b.textContent?.includes("Reached the confirmation page"));

    // Only the row actually holding cards claims a count; the earlier run row
    // stays silent rather than reading as "there might be something here".
    expect(rowHolding?.textContent).toContain("· 2 blocks");
    expect(screen.getAllByText(/· \d+ blocks?$/)).toHaveLength(1);

    fireEvent.click(rowHolding!);
    expect(screen.queryByText(/· \d+ blocks?$/)).toBeNull();
  });

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

    const gutter = screen.getAllByText(KIND_GLYPH_PATTERN);
    expect(gutter.map((g) => g.textContent)).toEqual(["⟨⟩", "▷"]);
    expect(screen.queryAllByRole("button", { expanded: true })).toHaveLength(0);
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
    const live = screen.getByRole("button", { expanded: true });

    fireEvent.click(live);
    expect(screen.queryAllByRole("button", { expanded: true })).toHaveLength(0);
  });

  it("a finished run row is one line, not a line plus a summary", () => {
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
    expect(screen.queryByText("Clicked the download link")).toBeNull();

    fireEvent.click(screen.getByTitle(/Highlight download_block/));
    expect(screen.getByText("Clicked the download link")).toBeTruthy();
  });

  it("the live run row is already open when its block lands", () => {
    // The in-flight call has nothing to show yet, so the row is a plain line.
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
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.getByText(/Testing workflow/)).toBeTruthy();

    // Once the block dispatches it anchors to that same row, and the row must
    // already be open — no click — or the running block card renders folded.
    const withBlock: TurnNarrativeState = {
      ...inFlight,
      blocks: [
        runningBlock({ workflowRunBlockId: "wrb_1", label: "download_block" }),
      ],
    };
    rerender(<NarrativeView turn={withBlock} />);
    expect(screen.getByRole("button", { expanded: true })).toBeTruthy();
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
    expect(screen.queryAllByText(/Opening page/)).toHaveLength(1);
  });

  it("uses the narrator's intent for a failed row and keeps the exact error in its detail", () => {
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
              outcomeLabel: "Ran it - everything passed",
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
    expect(screen.getByText("Running it")).toBeTruthy();
    expect(screen.getByText(/attempt failed/).className).not.toContain("rose");
    expect(
      screen.queryByText(
        "The submit button stayed disabled after filling the form",
      ),
    ).toBeNull();
    expect(screen.queryByText("Ran it - everything passed")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Running it/ }));
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

    expect(
      screen.getByText("Restoring access to the certification results")
        .className,
    ).not.toContain("rose");
    expect(screen.getByText(/attempt failed/).className).not.toContain("rose");
    expect(screen.getAllByText(/2 attempts/).length).toBeGreaterThan(0);
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

  it("shows an active repair's code before its tool result arrives", () => {
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

    expect(
      screen.getByRole("button", {
        name: "Expand code changes for add_to_cart",
      }),
    ).toBeTruthy();
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
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole("button", { name: /Repaired the download step/ }),
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
        screen.getByRole("button", { name: /Repaired the download step/ }),
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
    const currentRow = screen.getByRole("button", {
      name: /Repaired the download step/,
    });

    expect(container.querySelector("[data-code-diff-peek]")).toBeTruthy();
    fireEvent.click(currentRow);
    fireEvent.click(currentRow);

    expect(container.querySelector("[data-code-diff-peek]")).toBeNull();
    expect(screen.getByRole("button", { name: "view diff" })).toBeTruthy();
  });

  it("a historical write reveals its patch only after the row and diff are opened", () => {
    render(<NarrativeView turn={twoWriteTurn()} />);

    fireEvent.click(
      screen.getAllByRole("button", { name: /Saved and ran/ })[0]!,
    );

    expect(screen.queryByText("-await page.click('#a')")).toBeNull();
    expect(screen.getAllByRole("button", { name: "view diff" }).length).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "view diff" }));
    expect(screen.getByText("-await page.click('#a')")).toBeTruthy();
    expect(screen.getByText(/await page\.goto\(URL\)/)).toBeTruthy();
  });

  it("keeps collapsed diff counts quiet while preserving patch syntax colors", () => {
    render(<NarrativeView turn={twoWriteTurn({ terminal: "response" })} />);

    const addedCount = screen.getByText("+4");
    const removedCount = screen.getByText("−2");
    expect(addedCount.className).not.toContain("emerald");
    expect(removedCount.className).not.toContain("rose");

    fireEvent.click(
      screen.getAllByRole("button", { name: /Saved and ran/ })[0]!,
    );
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

    expect(container.querySelector("[data-code-diff-peek]")).toBeNull();
    expect(screen.queryByText("+await page.wait_for_timeout(500)")).toBeNull();
    // Counts stay on the collapsed row line; the patch is behind the expander.
    expect(screen.getByText("+4")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "view diff" })).toBeNull();

    fireEvent.click(
      screen.getAllByRole("button", { name: /Saved and ran/ })[0]!,
    );
    fireEvent.click(screen.getAllByRole("button", { name: "view diff" })[0]!);
    expect(screen.getByText("-await page.click('#a')")).toBeTruthy();
  });

  it("a payload without the new keys renders today's label card", () => {
    const turn = twoWriteTurn({ terminal: "response" });
    const legacy = {
      ...turn,
      designActivity: turn.designActivity.map(({ ...entry }) => {
        delete entry.codeDiffs;
        return entry;
      }),
    };
    render(<NarrativeView turn={legacy} />);

    expect(screen.getByText("Saved and ran the download step")).toBeTruthy();
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

    fireEvent.click(
      screen.getAllByRole("button", { name: /Saved and ran/ })[0]!,
    );
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
    fireEvent.click(
      screen.getAllByRole("button", { name: /Saved and ran/ })[0]!,
    );
    fireEvent.click(screen.getByRole("button", { name: "view diff" }));
    expect(screen.getByText("-await page.click('#a')")).toBeTruthy();
  });
});
