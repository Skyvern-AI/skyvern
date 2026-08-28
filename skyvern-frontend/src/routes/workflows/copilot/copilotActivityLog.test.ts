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

  it("keeps a failed browse action separate from preceding successful work", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "Opened the certificant search",
          success: true,
          iteration: 0,
        }),
        entry({
          id: "n-1",
          kind: "narration",
          text: "Opening the search form",
          iteration: 0,
          activeLabel: "Opening the certificant search",
          outcomeLabel: "Opened the certificant search",
        }),
        entry({
          id: "tr-2",
          kind: "tool_result",
          toolName: "get_page_evidence",
          text: "The browser target crashed",
          success: false,
          iteration: 1,
        }),
        entry({
          id: "n-2",
          kind: "narration",
          text: "Restoring the results page",
          iteration: 1,
          activeLabel: "Restoring access to the certification results",
          outcomeLabel: "Restored the certification results",
        }),
      ]),
    );

    expect(log.rows).toHaveLength(2);
    expect(log.rows.map((row) => row.label)).toEqual([
      "Opened the certificant search",
      "Restoring access to the certification results",
    ]);
    expect(log.rows[0]?.entries[0]?.success).toBe(true);
    expect(log.rows[1]?.entries[0]?.success).toBe(false);
  });

  it("coalesces differently implemented retries of one narrated browse activity", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "The browser target was unavailable",
          success: false,
          iteration: 0,
        }),
        entry({
          id: "n-1",
          kind: "narration",
          text: "Looking for the certification record",
          iteration: 0,
          activeLabel: "Searching for the certification record",
          outcomeLabel: "Found the certification record",
        }),
        entry({
          id: "tr-2",
          kind: "tool_result",
          toolName: "get_page_evidence",
          text: "The results page did not load",
          success: false,
          iteration: 1,
        }),
        entry({
          id: "n-2",
          kind: "narration",
          text: "Trying the search again",
          iteration: 1,
          activeLabel: "Searching for the certification record",
          outcomeLabel: "Found the certification record",
        }),
        entry({
          id: "tc-3",
          kind: "tool_call",
          toolName: "click_element",
          text: "Opening the search form",
          iteration: 2,
        }),
        entry({
          id: "n-3",
          kind: "narration",
          text: "Trying a direct search",
          iteration: 2,
          activeLabel: "Searching for the certification record",
          outcomeLabel: "Found the certification record",
        }),
      ]),
    );

    expect(log.rows).toHaveLength(1);
    expect(log.rows[0]).toMatchObject({
      id: "1",
      label: "Searching for the certification record",
      pending: true,
      live: true,
      startedAt: at(1),
      endedAt: at(3),
    });
    expect(log.rows[0]?.entries).toHaveLength(3);
    expect(log.rows[0]?.entries[2]?.attempts).toBe(3);
  });

  it("keeps overlapping browse siblings separate even with the same narrated intent", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-a",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "The first page failed",
          success: false,
          iteration: 0,
          activityStartedAt: "2026-01-01T00:00:10Z",
          timestamp: "2026-01-01T00:00:20Z",
        }),
        entry({
          id: "n-a",
          kind: "narration",
          text: "Checking the page",
          iteration: 0,
          activeLabel: "Searching the page",
          timestamp: "2026-01-01T00:00:20Z",
        }),
        entry({
          id: "tr-b",
          kind: "tool_result",
          toolName: "get_page_evidence",
          text: "The parallel page opened",
          success: true,
          iteration: 1,
          activityStartedAt: "2026-01-01T00:00:11Z",
          timestamp: "2026-01-01T00:00:21Z",
        }),
        entry({
          id: "n-b",
          kind: "narration",
          text: "Checking the same page",
          iteration: 1,
          activeLabel: "Searching the page",
          timestamp: "2026-01-01T00:00:21Z",
        }),
      ]),
    );

    expect(log.rows).toHaveLength(2);
    expect(log.rows.map((row) => row.entries.length)).toEqual([1, 1]);
    expect(log.rows.map((row) => row.id)).toEqual(["a", "b"]);
  });

  it("keeps browse tools from one iteration folded into one activity", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "Opened the search",
          success: true,
          iteration: 0,
        }),
        entry({
          id: "tr-2",
          kind: "tool_result",
          toolName: "get_page_evidence",
          text: "Read the search form",
          success: true,
          iteration: 0,
        }),
      ]),
    );

    expect(log.rows).toHaveLength(1);
    expect(log.rows[0]?.entries).toHaveLength(2);
  });

  it("keeps a code write visible in the immediate test frontier", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-write",
          kind: "tool_result",
          toolName: "add_block",
          text: "Added the cart block",
          success: true,
          iteration: 2,
          codeDiffs: [
            {
              label: "add_to_cart",
              added: 15,
              removed: 0,
              patch: "+await page.click('button.add-to-cart')",
            },
          ],
        }),
        entry({
          id: "tc-run",
          kind: "tool_call",
          toolName: "run_blocks_and_collect_debug",
          text: "Testing the cart block",
          iteration: 3,
        }),
      ]),
    );

    expect(log.rows).toHaveLength(1);
    expect(log.rows[0]?.kind).toBe("run");
    expect(log.rows[0]?.entries).toHaveLength(2);
    expect(log.rows[0]?.codeDiffs).toMatchObject([
      { label: "add_to_cart", added: 15, removed: 0 },
    ]);
    expect(log.focusIndex).toBe(0);
  });

  it("promotes an active block's repair diff into its frontier row", () => {
    const log = deriveActivityLog(
      turnWith(
        [],
        [
          block({
            label: "add_to_cart",
            state: "running",
            activity: [
              entry({
                id: "tc-repair",
                kind: "tool_call",
                toolName: "edit_block_and_run",
                text: "Repairing and testing the cart block",
                codeDiffs: [
                  {
                    label: "add_to_cart",
                    added: 2,
                    removed: 1,
                    patch: "-old\n+new",
                  },
                ],
              }),
            ],
          }),
        ],
      ),
    );

    expect(log.rows).toHaveLength(1);
    expect(log.rows[0]?.codeDiffs).toMatchObject([
      { label: "add_to_cart", added: 2, removed: 1, patch: "-old\n+new" },
    ]);
    expect(log.focusIndex).toBe(0);
  });

  it("does not merge an older parallel run into a later code write", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-write",
          kind: "tool_result",
          toolName: "add_block",
          text: "Added the cart block",
          success: true,
          timestamp: at(4),
          codeDiffs: [
            {
              label: "add_to_cart",
              added: 15,
              removed: 0,
              patch: "+await page.click('button.add-to-cart')",
            },
          ],
        }),
        entry({
          id: "tr-run",
          kind: "tool_result",
          toolName: "run_blocks_and_collect_debug",
          text: "An earlier parallel run finished",
          success: true,
          timestamp: at(5),
          activityStartedAt: at(2),
        }),
      ]),
    );

    expect(log.rows).toHaveLength(2);
    expect(log.rows.map((row) => row.kind)).toEqual(["author", "run"]);
  });

  it("anchors parallel block evidence by the run inside a combined write and test row", () => {
    const log = deriveActivityLog(
      turnWith(
        [
          entry({
            id: "tr-prior-run",
            kind: "tool_result",
            toolName: "run_blocks_and_collect_debug",
            text: "Finished the prior run",
            success: true,
            activityStartedAt: at(1),
            timestamp: at(3),
          }),
          entry({
            id: "tr-write",
            kind: "tool_result",
            toolName: "add_block",
            text: "Added a repaired block",
            success: true,
            timestamp: at(4),
            codeDiffs: [
              {
                label: "repaired_block",
                added: 3,
                removed: 0,
                patch: "+await page.goto(URL)",
              },
            ],
          }),
          entry({
            id: "tc-new-run",
            kind: "tool_call",
            toolName: "run_blocks_and_collect_debug",
            text: "Testing the repaired block",
            timestamp: at(6),
          }),
        ],
        [
          block({
            workflowRunBlockId: "wrb-prior",
            label: "prior_block",
            startedAt: at(5),
          }),
        ],
      ),
    );

    expect(log.rows).toHaveLength(2);
    expect(labelsPerRow(log)).toEqual([["prior_block"], []]);
  });

  it("anchors a block to the latest qualifying run start regardless of completion order", () => {
    const log = deriveActivityLog(
      turnWith(
        [
          entry({
            id: "tr-run-b",
            kind: "tool_result",
            toolName: "run_blocks_and_collect_debug",
            text: "Run B completed first",
            success: true,
            activityStartedAt: at(20),
            timestamp: at(30),
          }),
          entry({
            id: "tr-run-a",
            kind: "tool_result",
            toolName: "run_blocks_and_collect_debug",
            text: "Run A completed later",
            success: true,
            activityStartedAt: at(10),
            timestamp: at(40),
          }),
        ],
        [
          block({
            workflowRunBlockId: "wrb-b",
            label: "run_b_block",
            startedAt: at(25),
          }),
        ],
      ),
    );

    expect(labelsPerRow(log)).toEqual([["run_b_block"], []]);
  });

  it("keeps exact failed retry evidence inside a recovered block", () => {
    const log = deriveActivityLog(
      turnWith(
        [
          entry({
            id: "tr-run",
            kind: "tool_result",
            toolName: "run_blocks_and_collect_debug",
            text: "The block recovered",
            success: true,
          }),
        ],
        [
          block({
            workflowRunBlockId: "wrb-recovered",
            label: "recovered_block",
            state: "completed",
            activity: [
              entry({
                id: "tr-attempt-1",
                kind: "tool_result",
                toolName: "navigate_browser",
                text: "The browser target crashed",
                success: false,
                timestamp: at(2),
              }),
              entry({
                id: "tr-attempt-2",
                kind: "tool_result",
                toolName: "navigate_browser",
                text: "Opened the page after retrying",
                success: true,
                timestamp: at(3),
              }),
            ],
          }),
        ],
      ),
    );

    expect(log.rows[0]?.blocks[0]?.activity.map((entry) => entry.text)).toEqual(
      ["The browser target crashed", "Opened the page after retrying"],
    );
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
    expect(log.rows[0]?.entries).toHaveLength(2);
    expect(log.rows[0]?.entries[0]?.text).toBe("The login step timed out");
    expect(log.rows[0]?.entries[1]?.attempts).toBe(2);
    expect(log.rows[0]?.entries[1]?.text).toBe("Reached the confirmation page");
  });

  it("keeps a row's identity stable when a retry folds into it", () => {
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
    expect(idsOf(after)).toEqual(["1"]);
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

  it("attaches narration to the action in flight when its iteration trails the tool", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "click",
          text: "Opened the result",
          success: true,
          iteration: 2,
          timestamp: "2026-01-01T00:00:24Z",
        }),
        entry({
          id: "tc-inspect",
          kind: "tool_call",
          toolName: "inspect_page_for_composition",
          text: "Inspecting page",
          iteration: 3,
          timestamp: "2026-01-01T00:00:31Z",
        }),
        entry({
          id: "n-2",
          kind: "narration",
          text: "Reviewing the visible credential results",
          iteration: 2,
          activeLabel: "Reviewing the credential results",
          outcomeLabel: "Reviewed the credential results",
          timestamp: "2026-01-01T00:00:33Z",
        }),
        entry({
          id: "tr-inspect",
          kind: "tool_result",
          toolName: "inspect_page_for_composition",
          text: "The page could not be inspected",
          success: false,
          iteration: 3,
          timestamp: "2026-01-01T00:00:51Z",
        }),
      ]),
    );

    expect(log.rows).toHaveLength(2);
    expect(log.rows[0]?.reason).toBeNull();
    expect(log.rows[1]).toMatchObject({
      label: "Reviewing the credential results",
      reason: "Reviewing the visible credential results",
    });
  });

  it("does not let a stale pending call capture narration for a settled sibling", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tc-stale",
          kind: "tool_call",
          toolName: "update_workflow",
          text: "Opening the stale tab",
          iteration: 0,
          timestamp: "2026-01-01T00:00:10Z",
        }),
        entry({
          id: "tc-settled",
          kind: "tool_call",
          toolName: "get_page_evidence",
          text: "Inspecting the current page",
          iteration: 1,
          timestamp: "2026-01-01T00:00:20Z",
        }),
        entry({
          id: "tr-settled",
          kind: "tool_result",
          toolName: "get_page_evidence",
          text: "Read the current page",
          success: true,
          iteration: 1,
          timestamp: "2026-01-01T00:00:25Z",
        }),
        entry({
          id: "n-settled",
          kind: "narration",
          text: "Checking the current page for the result",
          iteration: 1,
          activeLabel: "Reviewing the current result",
          timestamp: "2026-01-01T00:00:26Z",
        }),
      ]),
    );

    expect(log.rows[0]?.reason).toBeNull();
    expect(log.rows[1]).toMatchObject({
      label: "Reviewing the current result",
      reason: "Checking the current page for the result",
    });
  });

  it("uses iteration when parallel pending calls have overlapping time spans", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tc-first",
          kind: "tool_call",
          toolName: "update_workflow",
          text: "Opening the first page",
          iteration: 0,
          timestamp: "2026-01-01T00:00:10Z",
        }),
        entry({
          id: "tc-second",
          kind: "tool_call",
          toolName: "get_page_evidence",
          text: "Inspecting the second page",
          iteration: 1,
          timestamp: "2026-01-01T00:00:20Z",
        }),
        entry({
          id: "n-first",
          kind: "narration",
          text: "Still opening the first page",
          iteration: 0,
          activeLabel: "Opening the first page",
          timestamp: "2026-01-01T00:00:21Z",
        }),
      ]),
    );

    expect(log.rows[0]).toMatchObject({
      label: "Opening the first page",
      reason: "Still opening the first page",
    });
    expect(log.rows[1]?.reason).toBeNull();
  });

  it("folds a technical recovery substep into the current narrated attempt", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-inspect",
          kind: "tool_result",
          toolName: "inspect_page_for_composition",
          text: "The page could not be inspected",
          success: false,
          iteration: 3,
        }),
        entry({
          id: "n-3",
          kind: "narration",
          text: "The result page stopped responding",
          iteration: 3,
          activeLabel: "Reviewing the credential results",
        }),
        entry({
          id: "tr-evaluate",
          kind: "tool_result",
          toolName: "evaluate",
          text: "The page was unavailable",
          success: false,
          iteration: 4,
        }),
      ]),
    );

    expect(log.rows).toHaveLength(1);
    expect(log.rows[0]).toMatchObject({
      label: "Reviewing the credential results",
      id: "inspect",
    });
    expect(log.rows[0]?.entries[1]?.attempts).toBe(2);
  });

  it("starts a new retry row after the prior narrated attempt recovered", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-failed",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "The page did not open",
          success: false,
          iteration: 0,
        }),
        entry({
          id: "n-failed",
          kind: "narration",
          text: "Trying the page",
          iteration: 0,
          activeLabel: "Opening the page",
        }),
        entry({
          id: "tr-recovered",
          kind: "tool_result",
          toolName: "get_page_evidence",
          text: "Read the page",
          success: true,
          iteration: 1,
        }),
        entry({
          id: "n-recovered",
          kind: "narration",
          text: "The page is available",
          iteration: 1,
          activeLabel: "Opening the page",
        }),
        entry({
          id: "tr-later",
          kind: "tool_result",
          toolName: "click_element",
          text: "The next click failed",
          success: false,
          iteration: 2,
        }),
        entry({
          id: "n-later",
          kind: "narration",
          text: "Trying the next control",
          iteration: 2,
          activeLabel: "Opening the page",
        }),
      ]),
    );

    expect(log.rows).toHaveLength(2);
    const recovered = log.rows[0]?.entries;
    const later = log.rows[1]?.entries;
    expect(recovered?.[recovered.length - 1]?.success).toBe(true);
    expect(later?.[later.length - 1]?.success).toBe(false);
  });

  it("recomputes pending and live after an out-of-order narrated retry merge", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tr-first",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "The first attempt failed",
          success: false,
          iteration: 0,
        }),
        entry({
          id: "n-first",
          kind: "narration",
          text: "Trying the search",
          iteration: 0,
          activeLabel: "Searching the page",
        }),
        entry({
          id: "tc-pending",
          kind: "tool_call",
          toolName: "get_page_evidence",
          text: "Inspecting the page",
          iteration: 1,
        }),
        entry({
          id: "n-pending",
          kind: "narration",
          text: "Trying the search again",
          iteration: 1,
          activeLabel: "Searching the page",
        }),
        entry({
          id: "tr-sibling",
          kind: "tool_result",
          toolName: "click_element",
          text: "The sibling attempt failed",
          success: false,
          iteration: 2,
        }),
        entry({
          id: "n-sibling",
          kind: "narration",
          text: "Still trying the search",
          iteration: 2,
          activeLabel: "Searching the page",
        }),
      ]),
    );

    expect(log.rows).toHaveLength(2);
    expect(log.rows[0]).toMatchObject({ pending: true, live: true });
    expect(log.rows[1]).toMatchObject({ pending: false, live: false });
    expect(log.liveIndex).toBe(0);
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

  it("keeps a failed row's active label instead of showing its predicted outcome", () => {
    const log = deriveActivityLog({
      ...turnWith([
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "update_and_run_blocks",
          text: "The submit button stayed disabled",
          success: false,
          iteration: 0,
        }),
        entry({
          id: "n-1",
          kind: "narration",
          text: "Confirming the form can be submitted",
          iteration: 0,
          activeLabel: "Testing form submission",
          outcomeLabel: "Confirmed the form submits successfully",
        }),
      ]),
      terminal: "response",
    });

    expect(log.rows[0]!.label).toBe("Testing form submission");
  });

  it("uses the outcome label after a retry recovers while preserving its failure", () => {
    const log = deriveActivityLog({
      ...turnWith([
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "The page did not open",
          success: false,
          iteration: 0,
        }),
        entry({
          id: "n-1",
          kind: "narration",
          text: "Trying the page",
          iteration: 0,
          activeLabel: "Opening the page",
          outcomeLabel: "Opened the page",
        }),
        entry({
          id: "tr-2",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "The page opened",
          success: true,
          iteration: 1,
        }),
        entry({
          id: "n-2",
          kind: "narration",
          text: "The retry reached the page",
          iteration: 1,
          activeLabel: "Opening the page",
          outcomeLabel: "Opened the page after retrying",
        }),
      ]),
      terminal: "response",
    });

    expect(log.rows).toHaveLength(1);
    expect(log.rows[0]?.label).toBe("Opened the page after retrying");
    expect(log.rows[0]?.entries[0]?.text).toBe("The page did not open");
    expect(log.rows[0]?.entries[1]?.text).toBe("The page opened");
  });

  it.each([
    ["stopped", undefined],
    ["completed", "evaluating"],
    ["completed", "not_demonstrated"],
  ] as const)(
    "keeps the active label for a non-success %s block with outcome %s",
    (state, outcome) => {
      const log = deriveActivityLog({
        ...turnWith(
          [
            entry({
              id: "tr-run",
              kind: "tool_result",
              toolName: "run_blocks_and_collect_debug",
              text: "The run returned",
              success: true,
              iteration: 0,
            }),
            entry({
              id: "n-run",
              kind: "narration",
              text: "Checking the run result",
              iteration: 0,
              activeLabel: "Checking the workflow",
              outcomeLabel: "Confirmed the workflow works",
            }),
          ],
          [block({ state, outcome })],
        ),
        terminal: "response",
      });

      expect(log.rows[0]?.label).toBe("Checking the workflow");
    },
  );

  it("uses the outcome label only when a block has a successful verdict", () => {
    const log = deriveActivityLog({
      ...turnWith(
        [
          entry({
            id: "tr-run",
            kind: "tool_result",
            toolName: "run_blocks_and_collect_debug",
            text: "The run returned",
            success: true,
            iteration: 0,
          }),
          entry({
            id: "n-run",
            kind: "narration",
            text: "Checking the run result",
            iteration: 0,
            activeLabel: "Checking the workflow",
            outcomeLabel: "Confirmed the workflow works",
          }),
        ],
        [block({ state: "completed", outcome: "demonstrated" })],
      ),
      terminal: "response",
    });

    expect(log.rows[0]?.label).toBe("Confirmed the workflow works");
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

  it("keeps a live row focused when an empty drafted block follows it", () => {
    const log = deriveActivityLog(
      turnWith(
        [
          entry({
            id: "tc-1",
            kind: "tool_call",
            toolName: "navigate_browser",
            displayLabel: "Searching the catalogue",
            iteration: 0,
          }),
        ],
        [
          block({
            workflowRunBlockId: "",
            label: "add_first_result",
            state: "drafted",
          }),
        ],
      ),
    );

    expect(log.rows[log.liveIndex]?.pending).toBe(true);
    expect(log.rows[log.rows.length - 1]?.blocks[0]?.state).toBe("drafted");
    expect(log.focusIndex).toBe(log.liveIndex);
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

  it("spans a settled action from its call timestamp to its result timestamp", () => {
    const log = deriveActivityLog(
      turnWith([
        entry({
          id: "tc-1",
          kind: "tool_call",
          toolName: "navigate_browser",
          text: "Opening page",
          timestamp: "2026-01-01T00:00:04Z",
        }),
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "The page did not open",
          success: false,
          timestamp: "2026-01-01T00:00:24Z",
        }),
      ]),
    );

    expect(log.rows[0]).toMatchObject({
      startedAt: "2026-01-01T00:00:04Z",
      endedAt: "2026-01-01T00:00:24Z",
    });
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
