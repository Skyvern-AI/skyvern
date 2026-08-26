import { describe, expect, it } from "vitest";

import {
  ACTIVITY_KIND_GLYPH,
  ActivityLog,
  deriveActivityLog,
  kindOf,
} from "./copilotActivityLog";
import {
  ActivityEntry,
  BlockState,
  EMPTY_NARRATIVE,
  TurnNarrativeState,
  applyNarrativeEvent,
  hydrateNarrativeFromPayload,
} from "./narrativeState";
import { WorkflowCopilotStreamResponseUpdate } from "./workflowCopilotTypes";

// Server clock, one second per step, so a fixture's happened-order and its
// timestamps agree without every entry spelling one out.
const at = (second: number): string =>
  `2026-01-01T00:00:${String(second).padStart(2, "0")}.000Z`;

const entry = (
  overrides: Partial<ActivityEntry> & Pick<ActivityEntry, "id" | "kind">,
): ActivityEntry => ({
  text: "…",
  iteration: 0,
  timestamp: at(Number(overrides.id.replace(/\D/g, "")) || 0),
  ...overrides,
});

const block = (overrides: Partial<BlockState> = {}): BlockState => ({
  workflowRunBlockId: "wrb_1",
  label: "block_1",
  blockType: "task",
  state: "completed",
  lastSeenIteration: 0,
  activity: [],
  startedAt: null,
  endedAt: null,
  ...overrides,
});

const turnWith = (
  designActivity: ActivityEntry[],
  blocks: BlockState[] = [],
): TurnNarrativeState => ({
  ...EMPTY_NARRATIVE,
  turnId: "turn-1",
  turnIndex: 0,
  designStarted: true,
  designActivity,
  blocks,
});

// browse → author → run-fail → browse → author → run
const repairLoopActivity = (): ActivityEntry[] => [
  entry({
    id: "tr-1",
    kind: "tool_result",
    toolName: "navigate_browser",
    text: "Opened the sign-in page",
    success: true,
  }),
  entry({
    id: "tr-2",
    kind: "tool_result",
    toolName: "update_workflow",
    text: "Saved 2 blocks",
    success: true,
  }),
  entry({
    id: "tr-3",
    kind: "tool_result",
    toolName: "update_and_run_blocks",
    text: "The submit button stayed disabled after filling the form",
    success: false,
    iteration: 1,
  }),
  entry({
    id: "tr-4",
    kind: "tool_result",
    toolName: "get_page_evidence",
    text: "Read the form state",
    success: true,
    iteration: 2,
  }),
  entry({
    id: "tr-5",
    kind: "tool_result",
    toolName: "update_workflow",
    text: "Saved 3 blocks",
    success: true,
    iteration: 2,
  }),
  entry({
    id: "tr-6",
    kind: "tool_result",
    toolName: "update_and_run_blocks",
    text: "Reached the confirmation page",
    success: true,
    iteration: 3,
  }),
];

// The failed run and its retry condense into one row, and folding keeps the
// successor, so that row reports the retry's timestamp rather than the failed
// attempt's. Only the first run keeps a row of its own.
const foldedRetryActivity = (): ActivityEntry[] => [
  entry({
    id: "tr-1",
    kind: "tool_result",
    toolName: "update_and_run_blocks",
    text: "Ran the first draft",
    success: true,
    iteration: 1,
  }),
  entry({
    id: "tr-2",
    kind: "tool_result",
    toolName: "update_and_run_blocks",
    text: "The submit button stayed disabled",
    success: false,
    iteration: 2,
  }),
  entry({
    id: "tr-3",
    kind: "tool_result",
    toolName: "update_and_run_blocks",
    text: "Reached the confirmation page",
    success: true,
    iteration: 3,
  }),
];

// The run is the pass's first completed tool round-trip, so it records
// iteration 0 — a real value, not a missing one.
const firstRoundTripActivity = (): ActivityEntry[] => [
  entry({
    id: "tr-1",
    kind: "tool_result",
    toolName: "update_and_run_blocks",
    text: "Ran the first draft",
    success: false,
    iteration: 0,
  }),
  entry({
    id: "tr-2",
    kind: "tool_result",
    toolName: "update_workflow",
    text: "Repaired the failing block",
    success: true,
    iteration: 1,
  }),
  entry({
    id: "tr-3",
    kind: "tool_result",
    toolName: "update_and_run_blocks",
    text: "Reached the confirmation page",
    success: true,
    iteration: 2,
  }),
];

// An enforcement nudge restarts stream_to_sse, so its iteration counter goes
// back to 0 partway through a turn whose rows keep accumulating.
const enforcementRestartActivity = (): ActivityEntry[] => [
  entry({
    id: "tr-1",
    kind: "tool_result",
    toolName: "update_and_run_blocks",
    text: "Ran before the nudge",
    success: false,
    iteration: 3,
  }),
  entry({
    id: "tr-2",
    kind: "tool_result",
    toolName: "get_page_evidence",
    text: "Read the form state",
    success: true,
    iteration: 0,
  }),
  entry({
    id: "tr-3",
    kind: "tool_result",
    toolName: "update_and_run_blocks",
    text: "Ran after the nudge",
    success: true,
    iteration: 1,
  }),
];

// A real turn carries both halves of each tool round-trip, and folding keeps
// the result — so a finished run row's surviving entry is stamped when the run
// ENDED. A block runs inside that window: call < block.startedAt < result.
const pairedRunActivity = (): ActivityEntry[] => [
  entry({
    id: "tc-1",
    kind: "tool_call",
    toolName: "update_and_run_blocks",
    displayLabel: "Testing workflow",
    timestamp: at(1),
  }),
  entry({
    id: "tr-1",
    kind: "tool_result",
    toolName: "update_and_run_blocks",
    text: "The submit button stayed disabled",
    success: false,
    timestamp: at(4),
  }),
  entry({
    id: "tc-2",
    kind: "tool_call",
    toolName: "get_page_evidence",
    displayLabel: "Reading the page",
    timestamp: at(5),
  }),
  entry({
    id: "tr-2",
    kind: "tool_result",
    toolName: "get_page_evidence",
    text: "Read the form state",
    success: true,
    timestamp: at(6),
  }),
  entry({
    id: "tc-3",
    kind: "tool_call",
    toolName: "update_and_run_blocks",
    displayLabel: "Testing workflow",
    timestamp: at(7),
  }),
  entry({
    id: "tr-3",
    kind: "tool_result",
    toolName: "update_and_run_blocks",
    text: "Reached the confirmation page",
    success: true,
    timestamp: at(10),
  }),
];

const payloadBlock = (
  label: string,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> => ({
  label,
  blockType: "task",
  state: "completed",
  lastSeenIteration: 0,
  activity: [],
  startedAt: null,
  endedAt: null,
  ...overrides,
});

const terminalResponse = (
  narrative_payload: Record<string, unknown>,
): WorkflowCopilotStreamResponseUpdate => ({
  type: "response",
  workflow_copilot_chat_id: "chat_1",
  message: "Done",
  response_time: "2026-06-10T00:01:00Z",
  proposal_disposition: "no_proposal",
  turn_id: "turn-1",
  narrative_payload,
});

// The run rows are tr-3 and tr-6; each block starts inside one of them.
const twoRunTurn = (): TurnNarrativeState =>
  turnWith(repairLoopActivity(), [
    block({
      workflowRunBlockId: "wrb_a",
      label: "first_attempt",
      state: "failed",
      startedAt: at(3),
    }),
    block({
      workflowRunBlockId: "wrb_b",
      label: "second_attempt",
      startedAt: at(6),
    }),
  ]);

const idsOf = (log: ActivityLog): string[] => log.rows.map((r) => r.id);

const labelsPerRow = (log: ActivityLog): string[][] =>
  log.rows.map((r) => r.blocks.map((b) => b.label));

const glyphsOf = (log: ActivityLog): string[] =>
  log.rows.map((r) => (r.kind === null ? "" : ACTIVITY_KIND_GLYPH[r.kind]));

describe("deriveActivityLog", () => {
  it("keeps a repair loop in happened-order with unique keys", () => {
    const log = deriveActivityLog(turnWith(repairLoopActivity()));

    expect(idsOf(log)).toEqual(["1", "2", "3", "4", "5", "6"]);
    expect(new Set(idsOf(log)).size).toBe(log.rows.length);
    expect(log.rows.map((r) => r.kind)).toEqual([
      "browse",
      "author",
      "run",
      "browse",
      "author",
      "run",
    ]);
    expect(glyphsOf(log)).toEqual(["◎", "⟨⟩", "▷", "◎", "⟨⟩", "▷"]);
  });

  it("never moves, re-identifies or re-opens an earlier row as the turn grows", () => {
    const full = repairLoopActivity();
    const finalRows = deriveActivityLog(turnWith(full)).rows;

    for (let k = 1; k <= full.length; k += 1) {
      const log = deriveActivityLog(turnWith(full.slice(0, k)));
      expect(log.rows).toEqual(finalRows.slice(0, k));
      expect(glyphsOf(log)).toEqual(
        finalRows
          .slice(0, k)
          .map((r) => (r.kind ? ACTIVITY_KIND_GLYPH[r.kind] : "")),
      );
    }
  });

  it("classifies update_and_run_blocks as a run, never as authoring", () => {
    const kind = kindOf(
      entry({
        id: "tr-1",
        kind: "tool_result",
        toolName: "update_and_run_blocks",
        text: "Reached the confirmation page",
        success: true,
      }),
    );

    expect(kind).toBe("run");
    expect(kind).not.toBe("author");
  });

  it("leaves narration rows without a kind", () => {
    expect(kindOf(entry({ id: "n-1", kind: "narration" }))).toBeNull();
  });

  it("classifies the block-scoped authoring tools as authoring", () => {
    const log = deriveActivityLog(
      turnWith(
        ["edit_block", "add_block", "delete_block"].map((toolName, i) =>
          entry({
            id: `tr-${i}`,
            kind: "tool_result",
            toolName,
            text: `Reworked block ${i}`,
            success: true,
            iteration: i,
          }),
        ),
      ),
    );

    expect(log.rows.map((r) => r.kind)).toEqual(["author", "author", "author"]);
    expect(glyphsOf(log)).toEqual(["⟨⟩", "⟨⟩", "⟨⟩"]);
  });

  it("folds an adjacent same-tool retry into one row carrying the attempt count", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "update_and_run_blocks",
          text: "The login step timed out",
          success: false,
        }),
        entry({
          id: "tr-2",
          kind: "tool_result",
          toolName: "update_and_run_blocks",
          text: "Reached the confirmation page",
          success: true,
          iteration: 1,
        }),
      ]),
    );

    expect(log.rows).toHaveLength(1);
    expect(log.rows[0]?.entries[0]?.attempts).toBe(2);
    expect(log.rows[0]?.entries[0]?.text).toBe("Reached the confirmation page");
  });

  it("re-keys a row when a retry folds into it, so a pin cannot survive the fold", () => {
    const attempt = entry({
      id: "tr-1",
      kind: "tool_result",
      toolName: "update_and_run_blocks",
      text: "The login step timed out",
      success: false,
    });
    const before = deriveActivityLog(turnWith([attempt]));
    const after = deriveActivityLog(
      turnWith([
        attempt,
        entry({
          id: "tr-2",
          kind: "tool_result",
          toolName: "update_and_run_blocks",
          text: "Reached the confirmation page",
          success: true,
          iteration: 1,
        }),
      ]),
    );

    expect(idsOf(before)).toEqual(["1"]);
    expect(idsOf(after)).toEqual(["2"]);
  });

  it("derives the same rows from a hydrated narrative payload as from the live turn", () => {
    const liveRows = deriveActivityLog(turnWith(repairLoopActivity())).rows;
    const hydrated = hydrateNarrativeFromPayload({
      turnId: "turn-1",
      turnIndex: 0,
      terminal: "response",
      designActivity: repairLoopActivity(),
    });

    expect(hydrated).toBeDefined();
    const hydratedLog = deriveActivityLog(hydrated!);

    expect(hydratedLog.rows).toEqual(liveRows);
    expect(glyphsOf(hydratedLog)).toEqual(["◎", "⟨⟩", "▷", "◎", "⟨⟩", "▷"]);
  });

  it("groups consecutive browse steps into one row and counts them", () => {
    const log = deriveActivityLog(
      turnWith(
        ["navigate_browser", "get_page_evidence", "click_element"].map(
          (toolName, i) =>
            entry({
              id: `tr-${i}`,
              kind: "tool_result",
              toolName,
              text: `Browse step ${i}`,
              success: true,
              iteration: i,
            }),
        ),
      ),
    );

    expect(log.rows).toHaveLength(1);
    expect(log.rows[0]?.kind).toBe("browse");
    expect(log.rows[0]?.entries).toHaveLength(3);
    expect(log.rows[0]?.entries[2]?.text).toBe("Browse step 2");
  });

  it("attaches a narration to the row owning its iteration, emitting no row of its own", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "Opened the sign-in page",
          success: true,
        }),
        entry({
          id: "tc-2",
          kind: "tool_call",
          toolName: "update_workflow",
          displayLabel: "Saving blocks",
          iteration: 1,
        }),
        entry({
          id: "n-1",
          kind: "narration",
          text: "Checking which fields the sign-in form needs",
          iteration: 0,
        }),
      ]),
    );

    expect(log.rows.map((r) => r.kind)).toEqual(["browse", "author"]);
    expect(
      log.rows.every((r) => r.entries.every((e) => e.kind !== "narration")),
    ).toBe(true);
    expect(log.rows[0]?.reason).toBe(
      "Checking which fields the sign-in form needs",
    );
    expect(log.rows[1]?.reason).toBeNull();
  });

  it("attaches an unmatched narration to the nearest preceding row", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "Opened the sign-in page",
          success: true,
          iteration: 0,
        }),
        entry({
          id: "n-1",
          kind: "narration",
          text: "Looking for the pricing table",
          iteration: 9,
        }),
      ]),
    );

    expect(log.rows.map((r) => r.kind)).toEqual(["browse"]);
    expect(log.rows[0]?.reason).toBe("Looking for the pricing table");
  });

  it("drops a narration with no preceding row rather than rendering one", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "n-1",
          kind: "narration",
          text: "Getting started",
          iteration: 4,
        }),
      ]),
    );

    expect(log.rows).toHaveLength(0);
  });

  it("keeps the latest narration when a merged browse row spans two iterations", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "Opened the sign-in page",
          success: true,
          iteration: 0,
        }),
        entry({
          id: "n-1",
          kind: "narration",
          text: "Finding the sign-in form",
          iteration: 0,
        }),
        entry({
          id: "tr-2",
          kind: "tool_result",
          toolName: "get_page_evidence",
          text: "Read the form state",
          success: true,
          iteration: 1,
        }),
        entry({
          id: "n-2",
          kind: "narration",
          text: "Confirming the form accepts an email",
          iteration: 1,
        }),
      ]),
    );

    expect(log.rows).toHaveLength(1);
    expect(log.rows[0]?.reason).toBe("Confirming the form accepts an email");
  });

  it("marks only the last unresolved tool call live when two are in flight", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tc-1",
          kind: "tool_call",
          toolName: "update_workflow",
          displayLabel: "Saving blocks",
        }),
        entry({
          id: "tc-2",
          kind: "tool_call",
          toolName: "update_and_run_blocks",
          displayLabel: "Testing workflow",
          iteration: 1,
        }),
      ]),
    );

    expect(log.rows).toHaveLength(2);
    expect(log.liveIndex).toBe(1);
  });

  it("keeps a row live while an earlier call is unresolved and a later one returned", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tc-1",
          kind: "tool_call",
          toolName: "navigate_browser",
          displayLabel: "Opening page",
        }),
        entry({
          id: "tc-2",
          kind: "tool_call",
          toolName: "get_page_evidence",
          displayLabel: "Reading page",
          iteration: 1,
        }),
        entry({
          id: "tr-2",
          kind: "tool_result",
          toolName: "get_page_evidence",
          text: "Read the form state",
          success: true,
          iteration: 1,
        }),
      ]),
    );

    expect(log.rows).toHaveLength(1);
    expect(log.rows[0]?.entries.map((e) => e.kind)).toEqual([
      "tool_call",
      "tool_result",
    ]);
    expect(log.rows[0]?.pending).toBe(true);
    expect(log.liveIndex).toBe(0);
  });

  it("leaves no row live once every call resolved and no block is running", () => {
    expect(deriveActivityLog(turnWith(repairLoopActivity())).liveIndex).toBe(
      -1,
    );
  });

  it("keeps a drafted block out of the run row and still renders it", () => {
    const log = deriveActivityLog(
      turnWith(repairLoopActivity(), [
        block({ state: "drafted", workflowRunBlockId: "", label: "block_2" }),
        block({ label: "block_1" }),
      ]),
    );

    const runRows = log.rows.filter((r) => r.kind === "run");
    expect(runRows[runRows.length - 1]?.blocks.map((b) => b.label)).toEqual([
      "block_1",
    ]);
    expect(runRows[0]?.blocks).toEqual([]);

    const draftedRow = log.rows[log.rows.length - 1];
    expect(draftedRow?.kind).toBe("author");
    expect(draftedRow?.entries).toEqual([]);
    expect(draftedRow?.blocks.map((b) => b.label)).toEqual(["block_2"]);
  });

  it("files each run's blocks under the run row that produced them", () => {
    const log = deriveActivityLog(twoRunTurn());

    expect(labelsPerRow(log)).toEqual([
      [],
      [],
      ["first_attempt"],
      [],
      [],
      ["second_attempt"],
    ]);
  });

  it("anchors a block on the folded retry row, not the run before it", () => {
    const log = deriveActivityLog(
      turnWith(foldedRetryActivity(), [
        block({
          workflowRunBlockId: "wrb_a",
          label: "first_pass",
          state: "failed",
          startedAt: at(1),
        }),
        block({
          workflowRunBlockId: "wrb_b",
          label: "retried",
          startedAt: at(3),
        }),
      ]),
    );

    expect(labelsPerRow(log)).toEqual([["first_pass"], ["retried"]]);
  });

  it("anchors on when a run started, not when it finished", () => {
    const log = deriveActivityLog(
      turnWith(pairedRunActivity(), [
        block({
          workflowRunBlockId: "wrb_a",
          label: "ran_in_first",
          state: "failed",
          startedAt: at(2),
        }),
        block({
          workflowRunBlockId: "wrb_b",
          label: "ran_in_second",
          startedAt: at(8),
        }),
      ]),
    );

    // Reading the row's own (result) stamp would put both blocks on the first
    // run row — a block always starts before its run reports back — and leave
    // the row that produced the second one empty.
    expect(labelsPerRow(log)).toEqual([
      ["ran_in_first"],
      [],
      ["ran_in_second"],
    ]);
  });

  it("files a block that ran in the first round-trip under its own run row", () => {
    const log = deriveActivityLog(
      turnWith(firstRoundTripActivity(), [
        block({
          workflowRunBlockId: "wrb_a",
          label: "ran_first",
          state: "failed",
          startedAt: at(1),
        }),
        block({
          workflowRunBlockId: "wrb_b",
          label: "ran_after_repair",
          startedAt: at(3),
        }),
      ]),
    );

    expect(labelsPerRow(log)).toEqual([
      ["ran_first"],
      [],
      ["ran_after_repair"],
    ]);
  });

  it("anchors correctly when iteration numbers restart mid-turn", () => {
    const log = deriveActivityLog(
      turnWith(enforcementRestartActivity(), [
        block({
          workflowRunBlockId: "wrb_a",
          label: "first_pass",
          state: "failed",
          startedAt: at(1),
        }),
        block({
          workflowRunBlockId: "wrb_b",
          label: "second_pass",
          startedAt: at(3),
        }),
      ]),
    );

    expect(labelsPerRow(log)).toEqual([["first_pass"], [], ["second_pass"]]);
  });

  it("reads a start time without an offset as UTC, not local time", () => {
    const log = deriveActivityLog(
      turnWith(repairLoopActivity(), [
        block({
          workflowRunBlockId: "wrb_b",
          label: "naive_stamp",
          startedAt: "2026-01-01T00:00:06",
        }),
      ]),
    );

    expect(labelsPerRow(log)).toEqual([[], [], [], [], [], ["naive_stamp"]]);
  });

  it("keeps a stale verdict off the row carrying the current one", () => {
    const log = deriveActivityLog(
      turnWith(repairLoopActivity(), [
        block({
          workflowRunBlockId: "wrb_a",
          label: "submit",
          state: "failed",
          startedAt: at(3),
        }),
        block({
          workflowRunBlockId: "wrb_b",
          label: "submit",
          state: "completed",
          startedAt: at(6),
        }),
      ]),
    );

    expect(labelsPerRow(log)).toEqual([[], [], ["submit"], [], [], ["submit"]]);
    expect(log.rows.map((r) => r.blocks.map((b) => b.state))).toEqual([
      [],
      [],
      ["failed"],
      [],
      [],
      ["completed"],
    ]);
  });

  it("anchors a block with no run identity by when it ran", () => {
    const log = deriveActivityLog(
      turnWith(repairLoopActivity(), [
        block({
          workflowRunBlockId: "",
          label: "unknown_run",
          startedAt: at(3),
        }),
      ]),
    );

    expect(labelsPerRow(log)).toEqual([[], [], ["unknown_run"], [], [], []]);
  });

  it("keeps a block whose own run row was evicted on the earliest surviving row", () => {
    const log = deriveActivityLog(
      turnWith(
        [
          entry({
            id: "tr-8",
            kind: "tool_result",
            toolName: "update_and_run_blocks",
            text: "Re-ran after the earlier rows aged out",
            success: false,
          }),
          entry({
            id: "tr-9",
            kind: "tool_result",
            toolName: "get_page_evidence",
            text: "Checked the result",
            success: true,
          }),
          entry({
            id: "tr-10",
            kind: "tool_result",
            toolName: "update_and_run_blocks",
            text: "Ran once more",
            success: true,
          }),
        ],
        [
          // Ran before every row that survived the activity cap, so no row can
          // claim it. It belongs on the nearest survivor — the earliest one —
          // not the newest, which is the furthest from where it actually ran.
          block({
            workflowRunBlockId: "wrb_a",
            label: "evicted_run",
            startedAt: at(2),
          }),
        ],
      ),
    );

    expect(labelsPerRow(log)).toEqual([["evicted_run"], [], []]);
    expect(log.rows).toHaveLength(3);
  });

  it("keeps a block with no recorded start time on the last run row", () => {
    const log = deriveActivityLog(
      turnWith(repairLoopActivity(), [
        block({
          workflowRunBlockId: "wrb_a",
          label: "first_attempt",
          state: "failed",
          startedAt: null,
        }),
        block({
          workflowRunBlockId: "wrb_b",
          label: "second_attempt",
          startedAt: null,
        }),
      ]),
    );

    expect(labelsPerRow(log)).toEqual([
      [],
      [],
      [],
      [],
      [],
      ["first_attempt", "second_attempt"],
    ]);
    expect(log.rows).toHaveLength(6);
  });

  it("anchors a reloaded turn exactly like the live one", () => {
    const live = twoRunTurn();
    const reloaded = applyNarrativeEvent(
      live,
      terminalResponse({
        turnId: "turn-1",
        turnIndex: 0,
        terminal: "response",
        designActivity: repairLoopActivity(),
        blocks: [
          payloadBlock("first_attempt", {
            workflowRunBlockId: "wrb_a",
            state: "failed",
            startedAt: at(3),
          }),
          payloadBlock("second_attempt", {
            workflowRunBlockId: "wrb_b",
            startedAt: at(6),
          }),
        ],
      }),
    );

    expect(labelsPerRow(deriveActivityLog(live))).toEqual([
      [],
      [],
      ["first_attempt"],
      [],
      [],
      ["second_attempt"],
    ]);
    expect(labelsPerRow(deriveActivityLog(reloaded))).toEqual(
      labelsPerRow(deriveActivityLog(live)),
    );
  });

  it("degrades a reloaded block whose start time was not persisted", () => {
    const reloaded = applyNarrativeEvent(
      twoRunTurn(),
      terminalResponse({
        turnId: "turn-1",
        turnIndex: 0,
        terminal: "response",
        designActivity: repairLoopActivity(),
        blocks: [
          payloadBlock("first_attempt", { state: "failed" }),
          payloadBlock("second_attempt", {
            workflowRunBlockId: "wrb_b",
            startedAt: null,
          }),
        ],
      }),
    );

    expect(reloaded.blocks[0]?.startedAt).toBeNull();

    const log = deriveActivityLog(reloaded);
    expect(labelsPerRow(log)).toEqual([
      [],
      [],
      [],
      [],
      [],
      ["first_attempt", "second_attempt"],
    ]);
    expect(log.rows).toHaveLength(6);
  });

  it("gives a block its own run row when the turn has no run row to anchor it", () => {
    const log = deriveActivityLog(
      turnWith(
        [
          entry({
            id: "tr-1",
            kind: "tool_result",
            toolName: "update_workflow",
            text: "Saved 2 blocks",
            success: true,
          }),
        ],
        [block()],
      ),
    );

    expect(log.rows[0]?.blocks).toEqual([]);
    expect(log.rows).toHaveLength(2);
    expect(log.rows[1]?.kind).toBe("run");
    expect(log.rows[1]?.blocks.map((b) => b.label)).toEqual(["block_1"]);
    expect(log.liveIndex).toBe(-1);
  });

  it("keeps same-label blocks apart so loop iterations stay distinct", () => {
    const log = deriveActivityLog({
      ...turnWith([
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "update_and_run_blocks",
          text: "Ran it",
          success: true,
        }),
      ]),
      blocks: [
        block({ workflowRunBlockId: "wrb_a", label: "step", state: "failed" }),
        block({
          workflowRunBlockId: "wrb_b",
          label: "step",
          state: "completed",
        }),
      ],
    });

    const anchored = log.rows.flatMap((r) => r.blocks);
    expect(anchored.map((b) => b.workflowRunBlockId)).toEqual([
      "wrb_a",
      "wrb_b",
    ]);
  });

  it("folds every row once the turn ends, even with a call left unmatched", () => {
    const log = deriveActivityLog({
      ...turnWith([
        entry({
          id: "tc-1",
          kind: "tool_call",
          toolName: "navigate_browser",
          text: "Opening page…",
        }),
      ]),
      terminal: "response",
    });

    expect(log.rows[0]?.pending).toBe(true);
    expect(log.liveIndex).toBe(-1);
  });

  it("pairs a second pass's narration to that pass's step, not the first pass's", () => {
    // iteration restarts at 0 each enforcement pass while designActivity accumulates.
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-p1",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "Opened the catalogue",
          success: true,
          iteration: 0,
        }),
        entry({
          id: "n-p1",
          kind: "narration",
          text: "Checking whether the invoices need a login",
          iteration: 0,
        }),
        entry({
          id: "tr-p2",
          kind: "tool_result",
          toolName: "update_workflow",
          text: "Saved 2 blocks",
          success: true,
          iteration: 0,
        }),
        entry({
          id: "n-p2",
          kind: "narration",
          text: "Saving so the run has steps to execute",
          iteration: 0,
        }),
      ]),
    );

    expect(log.rows[0]!.reason).toBe(
      "Checking whether the invoices need a login",
    );
    expect(log.rows[1]!.reason).toBe("Saving so the run has steps to execute");
  });

  it("titles a finished row with the outcome tense and a live row with the active one", () => {
    const activity = [
      entry({
        id: "tr-1",
        kind: "tool_result",
        toolName: "navigate_browser",
        text: "Opened the catalogue",
        success: true,
        iteration: 0,
      }),
      entry({
        id: "n-1",
        kind: "narration",
        text: "Checking whether the invoices need a login",
        iteration: 0,
        activeLabel: "Looking for the invoice list",
        outcomeLabel: "Found the invoices under Billing History",
      }),
    ];

    const finished = deriveActivityLog({
      ...turnWith(activity),
      terminal: "response",
    });
    expect(finished.rows[0]!.label).toBe(
      "Found the invoices under Billing History",
    );

    const live = deriveActivityLog(
      turnWith([
        entry({
          id: "tc-1",
          kind: "tool_call",
          toolName: "navigate_browser",
          text: "Opening…",
          iteration: 0,
        }),
        entry({
          id: "n-1",
          kind: "narration",
          text: "Checking whether the invoices need a login",
          iteration: 0,
          activeLabel: "Looking for the invoice list",
          outcomeLabel: "Found the invoices under Billing History",
        }),
      ]),
    );
    expect(live.liveIndex).toBe(0);
    expect(live.rows[0]!.label).toBe("Looking for the invoice list");
  });

  it("leaves the row label null when the narrator never spoke for the step", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "Opened the catalogue",
          success: true,
        }),
      ]),
    );

    // Null, not empty: the row falls back to its tool-derived title.
    expect(log.rows[0]!.label).toBeNull();
  });

  it("pairs a narration that arrived before its own tool_result to that step, not the previous one", () => {
    // The live ordering for any tool slower than the narrator: call, narration,
    // then result. condenseActivityEntries moves the result after the narration,
    // so the owning row does not exist yet when the narration is seen.
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-0",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "Opened the catalogue",
          success: true,
          iteration: 0,
        }),
        entry({
          id: "tc-1",
          kind: "tool_call",
          toolName: "update_workflow",
          text: "Updating…",
          iteration: 1,
        }),
        entry({
          id: "n-1",
          kind: "narration",
          text: "Saving so the run has steps",
          iteration: 1,
          activeLabel: "Saving the workflow",
          outcomeLabel: "Saved 2 blocks",
        }),
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "update_workflow",
          text: "Saved",
          success: true,
          iteration: 1,
        }),
      ]),
    );

    const browse = log.rows.find((r) => r.kind === "browse")!;
    const author = log.rows.find((r) => r.kind === "author")!;
    expect(browse.reason).toBeNull();
    expect(browse.label).toBeNull();
    expect(author.reason).toBe("Saving so the run has steps");
  });

  it("keeps a narration that opened the turn, before any row existed", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "n-0",
          kind: "narration",
          text: "Why we start here",
          iteration: 0,
          activeLabel: "Opening the catalogue",
          outcomeLabel: "Opened the catalogue",
        }),
        entry({
          id: "tr-0",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "Opened",
          success: true,
          iteration: 0,
        }),
      ]),
    );

    expect(log.rows[0]!.reason).toBe("Why we start here");
    expect(log.rows[0]!.label).toBe("Opened the catalogue");
  });

  it("keeps the active tense on a step that never returned, even after the turn ended", () => {
    // liveIndex is -1 once the turn is terminal, so a row with an unmatched
    // call would otherwise be titled as if it had finished.
    const log = deriveActivityLog({
      ...turnWith([
        entry({
          id: "tc-1",
          kind: "tool_call",
          toolName: "update_workflow",
          text: "Updating…",
          iteration: 0,
        }),
        entry({
          id: "n-1",
          kind: "narration",
          text: "Saving so the run has steps",
          iteration: 0,
          activeLabel: "Saving the workflow",
          outcomeLabel: "Saved 2 blocks",
        }),
      ]),
      terminal: "response",
    });

    expect(log.liveIndex).toBe(-1);
    expect(log.rows[0]!.pending).toBe(true);
    expect(log.rows[0]!.label).toBe("Saving the workflow");
  });

  it("marks a row live while its call is unresolved, and not after the turn ends", () => {
    const activity = [
      entry({
        id: "tc-1",
        kind: "tool_call",
        toolName: "navigate_browser",
        iteration: 0,
      }),
    ];

    const running = deriveActivityLog(turnWith(activity));
    expect(running.rows[0]!.live).toBe(true);

    // A cancelled or timed-out turn can end with a call still unmatched. The
    // clock is driven off this flag, so it has to stop even then.
    const ended = deriveActivityLog({
      ...turnWith(activity),
      terminal: { kind: "completed", text: "done" },
    } as never);
    expect(ended.rows[0]!.pending).toBe(true);
    expect(ended.rows[0]!.live).toBe(false);
  });

  it("keeps the newest row in focus while the model is generating", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "Opened the page",
          success: true,
          iteration: 0,
        }),
      ]),
    );

    // No unmatched call and no running block, but the turn has not ended: the
    // gap between one call returning and the next being made used to leave
    // liveIndex at -1, so nothing was open and the log looked idle.
    expect(log.liveIndex).toBe(-1);
    expect(log.focusIndex).toBe(log.rows.length - 1);
  });

  it("does not move focus back to an earlier row when a later one settles", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tc-1",
          kind: "tool_call",
          toolName: "navigate_browser",
          iteration: 0,
        }),
        entry({
          id: "tr-2",
          kind: "tool_result",
          toolName: "update_and_run_blocks",
          text: "Ran the workflow",
          success: true,
          iteration: 1,
        }),
      ]),
    );

    // The earlier row is the one still calling, so strict liveness points back
    // at it — following that is what collapsed the row being read whenever a
    // parallel call finished. Focus only ever moves forward.
    expect(log.rows.length).toBeGreaterThan(1);
    expect(log.focusIndex).toBe(log.rows.length - 1);
  });

  it("keeps the title a row was introduced with when a later narration lands", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "Opened the catalogue",
          success: true,
          iteration: 0,
        }),
        entry({
          id: "n-1",
          kind: "narration",
          text: "Checking whether the invoices need a login",
          iteration: 0,
          activeLabel: "Looking for the invoices",
          outcomeLabel: "Found the invoices",
        }),
        entry({
          id: "n-2",
          kind: "narration",
          text: "Now confirming the prices are listed",
          iteration: 0,
        }),
      ]),
    );

    // The reason tracks the latest narration, but the title does not: a line
    // the user is already reading should not be reworded underneath them.
    expect(log.rows[0]!.reason).toBe("Now confirming the prices are listed");
    expect(log.rows[0]!.label).toBe("Found the invoices");
  });

  it("lets a later narration settle the outcome without renaming the live work", () => {
    const rows = (pending: boolean) =>
      deriveActivityLog(
        turnWith([
          entry({
            id: "tc-1",
            kind: "tool_call",
            toolName: "extract_data",
            iteration: 0,
          }),
          entry({
            id: "n-1",
            kind: "narration",
            text: "Reading the star count",
            iteration: 0,
            activeLabel: "Reading the star count",
            outcomeLabel: "Read the star count",
          }),
          entry({
            id: "n-2",
            kind: "narration",
            text: "Confirming the number is current",
            iteration: 0,
            activeLabel: "Confirming the number",
            outcomeLabel: "Confirmed the star count",
          }),
          ...(pending
            ? []
            : [
                entry({
                  id: "tr-1",
                  kind: "tool_result",
                  toolName: "extract_data",
                  text: "Extracted",
                  success: true,
                  iteration: 0,
                }),
              ]),
        ]),
      ).rows[0]!;

    // Live: the first narration named the work, so the second cannot rename it.
    expect(rows(true).label).toBe("Reading the star count");
    // Finished: the latest narration is the one that knows how it ended.
    expect(rows(false).label).toBe("Confirmed the star count");
  });

  it("spans a merged row from its first entry stamp to its last", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "Opened",
          success: true,
          iteration: 0,
          timestamp: "2026-01-01T00:00:00+00:00",
        }),
        entry({
          id: "tr-2",
          kind: "tool_result",
          toolName: "get_page_evidence",
          text: "Read it",
          success: true,
          iteration: 1,
          timestamp: "2026-01-01T00:00:14+00:00",
        }),
      ]),
    );

    // One browse row absorbing both entries reports the whole span, not one instant.
    expect(log.rows).toHaveLength(1);
    expect(log.rows[0]!.startedAt).toBe("2026-01-01T00:00:00+00:00");
    expect(log.rows[0]!.endedAt).toBe("2026-01-01T00:00:14+00:00");
  });

  it("leaves the span null against a backend that does not stamp entries", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "Opened",
          success: true,
          timestamp: undefined,
        }),
      ]),
    );

    expect(log.rows[0]!.startedAt).toBeNull();
    expect(log.rows[0]!.endedAt).toBeNull();
  });
});
