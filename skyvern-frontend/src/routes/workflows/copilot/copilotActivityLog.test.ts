import { describe, expect, it } from "vitest";

import {
  ActivityLog,
  callRollup,
  deriveActivityLog,
  kindOf,
  summarizeFinishedTurn,
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

// Stamps a fixture in the order it happened, one second apart.
const inOrder = (entries: ActivityEntry[]): ActivityEntry[] =>
  entries.map((e, i) => ({ ...e, timestamp: at(i) }));

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
  });

  it("never moves, re-identifies or re-opens an earlier row as the turn grows", () => {
    const full = repairLoopActivity();
    const finalRows = deriveActivityLog({
      ...turnWith(full),
      terminal: "response",
    }).rows;

    for (let k = 1; k <= full.length; k += 1) {
      const log = deriveActivityLog(turnWith(full.slice(0, k)));
      expect(log.rows.slice(0, -1)).toEqual(finalRows.slice(0, k - 1));
      // The newest step is the one still being worked on.
      expect({ ...log.rows[log.rows.length - 1]!, live: false }).toEqual(
        finalRows[k - 1],
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
        }),
      ]),
    );

    expect(log.rows).toHaveLength(2);
    expect(log.rows.map((row) => row.reason)).toEqual([
      "Opening the search form",
      "Restoring the results page",
    ]);
    expect(log.rows[0]?.entries[0]?.success).toBe(true);
    expect(log.rows[1]?.entries[0]?.success).toBe(false);
  });

  it("keeps each narrated retry of one browse activity as its own step, in order", () => {
    const log = deriveActivityLog(
      turnWith(
        inOrder([
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
          }),
        ]),
      ),
    );

    expect(log.rows.map((row) => row.reason)).toEqual([
      "Looking for the certification record",
      "Trying the search again",
      "Trying a direct search",
    ]);
    expect(log.rows.map((row) => row.live)).toEqual([false, false, true]);
    expect(log.rows[2]).toMatchObject({
      pending: true,
    });
  });

  it("keeps overlapping browse siblings separate and opens no line over them while they run", () => {
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

    expect(log.rows.map((row) => row.id)).toEqual(["a", "b"]);
    expect(log.rows.map((row) => row.entries.length)).toEqual([1, 1]);
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
    const liveRows = deriveActivityLog({
      ...turnWith(repairLoopActivity()),
      terminal: "response",
    }).rows;
    const hydrated = hydrateNarrativeFromPayload({
      turnId: "turn-1",
      turnIndex: 0,
      terminal: "response",
      designActivity: repairLoopActivity(),
    });

    expect(hydrated).toBeDefined();
    const hydratedLog = deriveActivityLog(hydrated!);

    expect(hydratedLog.rows).toEqual(liveRows);
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

  it("attaches a lagging narration to the call in flight when it was spoken", () => {
    const log = deriveActivityLog(
      turnWith(
        inOrder([
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
      ),
    );

    expect(log.rows.map((r) => r.kind)).toEqual(["browse", "author"]);
    expect(
      log.rows.every((r) => r.entries.every((e) => e.kind !== "narration")),
    ).toBe(true);
    expect(log.rows[0]?.reason).toBeNull();
    expect(log.rows[1]?.reason).toBe(
      "Checking which fields the sign-in form needs",
    );
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
      reason: "Checking the current page for the result",
    });
  });

  it("attaches a narration to its own step when parallel calls are both in flight", () => {
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

    expect(log.rows).toHaveLength(2);
    expect(log.rows[0]).toMatchObject({
      reason: "Still opening the first page",
    });
    expect(log.rows[1]?.reason).toBeNull();
  });

  it("folds a technical recovery substep into the current narrated attempt", () => {
    const log = deriveActivityLog(
      turnWith(
        inOrder([
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
      ),
    );

    expect(log.rows).toHaveLength(1);
    expect(log.rows[0]).toMatchObject({
      id: "inspect",
    });
    expect(log.rows[0]?.entries[1]?.attempts).toBe(2);
  });

  it("keeps each narrated attempt as its own step", () => {
    const log = deriveActivityLog(
      turnWith(
        inOrder([
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
      ),
    );

    expect(log.rows.map((row) => row.entries[0]?.success)).toEqual([
      false,
      true,
      false,
    ]);
  });

  it("keeps a pending attempt live while a later attempt settles", () => {
    const log = deriveActivityLog(
      turnWith(
        inOrder([
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
      ),
    );

    expect(log.rows).toHaveLength(3);
    expect(log.rows[1]).toMatchObject({ pending: true, live: true });
    expect(log.rows[2]).toMatchObject({ pending: false, live: false });
    expect(log.liveIndex).toBe(1);
  });

  it("renders a narration that arrives before any call as its own line", () => {
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

    expect(log.rows).toHaveLength(1);
    expect(log.rows[0]).toMatchObject({
      reason: "Getting started",
    });
    expect(log.rows[0]?.entries).toHaveLength(0);
  });

  it("gives each narrated browse iteration its own step and reason", () => {
    const log = deriveActivityLog(
      turnWith(
        inOrder([
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
      ),
    );

    expect(log.rows.map((row) => row.reason)).toEqual([
      "Finding the sign-in form",
      "Confirming the form accepts an email",
    ]);
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

  it("keeps the newest step live between calls and nothing live once the turn ends", () => {
    const rows = repairLoopActivity();
    expect(deriveActivityLog(turnWith(rows)).liveIndex).toBe(5);
    expect(
      deriveActivityLog({ ...turnWith(rows), terminal: "response" }).liveIndex,
    ).toBe(-1);
  });

  it("does not project an evidence-free drafted block", () => {
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

    expect(
      log.rows.some((row) => row.blocks.some((b) => b.label === "block_2")),
    ).toBe(false);
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

  it("drops a legacy row with no persisted evidence", () => {
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
    expect(labelsPerRow(log)).toEqual([[], [], [], [], [], ["second_attempt"]]);
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
    expect(log.rows[1]?.live).toBe(false);
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

  it("keeps a retry announced between attempts as its own step, preserving the failure", () => {
    const log = deriveActivityLog({
      ...turnWith(
        inOrder([
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
          }),
        ]),
      ),
      terminal: "response",
    });

    // The narrator spoke between the two attempts, so the retry is its own
    // step and the failure stays where the reader saw it.
    expect(
      log.rows.map((row) => [row.reason, row.entries.map((e) => e.text)]),
    ).toEqual([
      ["Trying the page", ["The page did not open"]],
      ["The retry reached the page", ["The page opened"]],
    ]);
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
    expect(author.reason).toBe("Saving so the run has steps");
  });

  it("keeps a narration that opened the turn, before any row existed", () => {
    const log = deriveActivityLog({
      ...turnWith([
        entry({
          id: "n-0",
          kind: "narration",
          text: "Why we start here",
          iteration: 0,
          activeLabel: "Opening the catalogue",
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
      terminal: "response",
    });

    expect(log.rows[0]!.reason).toBe("Why we start here");
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
    // model is between calls, so the newest step is still the live one.
    expect(log.liveIndex).toBe(0);
    expect(log.focusIndex).toBe(log.rows.length - 1);
  });

  it("keeps a live row focused when state contains an evidence-free drafted block", () => {
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
    expect(
      log.rows.some((row) => row.blocks.some((b) => b.state === "drafted")),
    ).toBe(false);
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

  it("never rewrites a step when a later narration lands", () => {
    const log = deriveActivityLog({
      ...turnWith(
        inOrder([
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
          }),
          entry({
            id: "n-2",
            kind: "narration",
            text: "Now confirming the prices are listed",
            iteration: 0,
          }),
        ]),
      ),
      terminal: "response",
    });

    expect(log.rows[0]).toMatchObject({
      reason: "Checking whether the invoices need a login",
    });
    expect(log.rows[1]).toMatchObject({
      reason: "Now confirming the prices are listed",
      entries: [],
    });
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

// A browsing turn where the narrator speaks roughly once per call, in the
// order a live stream delivered it (narrations tagged with the iteration they
// explain, one arriving while its call was still in flight).
const narratedBrowsingActivity = (): ActivityEntry[] => {
  const call = (n: number, iteration: number, toolName: string) => [
    entry({ id: `tc-${n}`, kind: "tool_call", iteration, toolName }),
    entry({
      id: `tr-${n}`,
      kind: "tool_result",
      iteration,
      toolName,
      success: true,
    }),
  ];
  const narration = (n: number, iteration: number, activeLabel: string) =>
    entry({
      id: `n-${n}`,
      kind: "narration",
      iteration,
      text: `Reason ${n}`,
      activeLabel,
    });
  const [tc3, tr3] = call(3, 3, "evaluate");
  return [
    ...call(0, 0, "set_work_plan"),
    narration(1, 0, "Checking the quote"),
    ...call(1, 1, "navigate_browser"),
    narration(2, 1, "Opening the quote site"),
    ...call(2, 2, "inspect_page_for_composition"),
    narration(3, 1, "Looking for the price"),
    tc3!,
    narration(4, 3, "Checking the current price"),
    tr3!,
    ...call(4, 4, "navigate_browser"),
    narration(5, 4, "Checking the quote again"),
    ...call(5, 5, "inspect_page_for_composition"),
    ...call(6, 6, "evaluate"),
  ].map((e, i) => ({ ...e, timestamp: at(i) }));
};

describe("deriveActivityLog — narrated browsing", () => {
  it("opens a step for each narration in the order it was spoken", () => {
    const log = deriveActivityLog({
      ...turnWith(narratedBrowsingActivity()),
      terminal: "response",
    });
    // The plan call returned before the first sentence, which is about the
    // navigation after it.
    expect(log.rows.map((row) => row.reason)).toEqual([
      null,
      "Reason 1",
      "Reason 2",
      "Reason 3",
      "Reason 4",
      "Reason 5",
    ]);
    // Calls spoken after a narration join its line; nothing moves back.
    expect(log.rows.map((row) => row.entries.map((e) => e.id))).toEqual([
      ["tr-0"],
      ["tr-1"],
      ["tr-2"],
      ["tr-3"],
      ["tr-4"],
      ["tr-5", "tr-6"],
    ]);
  });

  it("keeps the newest step working between calls until a narration or a later step moves on", () => {
    const activity = narratedBrowsingActivity();
    const outcome = entry({
      id: "n-9",
      kind: "narration",
      iteration: 1,
      text: "Reason 9",
      activeLabel: "Opening the quote site",
      timestamp: "2026-01-01T00:00:05.500Z",
    });
    // The navigation returned, but the model has not started another call.
    const between = deriveActivityLog(turnWith(activity.slice(0, 5)));
    expect(between.rows.map((row) => row.live)).toEqual([false, true]);

    // The narrator names the outcome on a line of its own, so the step above
    // stops spinning instead of loading over the new sentence.
    const next = deriveActivityLog(
      turnWith([...activity.slice(0, 5), outcome]),
    );
    expect(next.rows.map((row) => [row.reason, row.live])).toEqual([
      [null, false],
      ["Reason 1", false],
      ["Reason 9", false],
    ]);
  });
});

// The shape of a captured live browsing turn: narrations arrive seconds after
// the call they describe, tagged with an iteration that trails the work.
const laggingNarrationActivity = (): ActivityEntry[] => {
  const t = (s: number) =>
    new Date(Date.UTC(2026, 0, 1, 0, 0, s)).toISOString();
  const tool = (
    kind: "tool_call" | "tool_result",
    n: number,
    toolName: string,
    s: number,
  ) =>
    entry({
      id: `${kind === "tool_call" ? "tc" : "tr"}-${n}`,
      kind,
      toolName,
      iteration: n,
      success: kind === "tool_result" ? true : undefined,
      timestamp: t(s),
    });
  const said = (n: string, iteration: number, s: number) =>
    entry({
      id: `n-${n}`,
      kind: "narration",
      iteration,
      text: `Reason ${n}`,
      activeLabel: `Intent ${n}`,
      timestamp: t(s),
    });
  return [
    tool("tool_call", 0, "set_work_plan", 3),
    tool("tool_result", 0, "set_work_plan", 4),
    said("a", 0, 6),
    tool("tool_call", 1, "navigate_browser", 7),
    tool("tool_result", 1, "navigate_browser", 63),
    said("b", 1, 66),
    tool("tool_call", 2, "inspect_page_for_composition", 67),
    tool("tool_result", 2, "inspect_page_for_composition", 82),
    said("c", 1, 99),
    tool("tool_call", 3, "evaluate", 118),
    said("d", 3, 130),
    tool("tool_result", 3, "evaluate", 141),
    said("e", 3, 144),
    tool("tool_call", 4, "navigate_browser", 144),
    tool("tool_result", 4, "navigate_browser", 200),
    said("f", 4, 203),
    tool("tool_call", 5, "inspect_page_for_composition", 203),
    tool("tool_result", 5, "inspect_page_for_composition", 215),
    said("g", 4, 218),
    tool("tool_call", 6, "evaluate", 219),
    tool("tool_result", 6, "evaluate", 227),
  ];
};

describe("deriveActivityLog — drafting", () => {
  it("leaves only the drafting row live once the step before it settled", () => {
    const log = deriveActivityLog({
      ...turnWith([
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "navigate_browser",
          text: "Opened the page",
          success: true,
        }),
      ]),
      codegenProgress: { blockLabels: ["open_page"] },
    });

    expect(log.rows.map((row) => row.live)).toEqual([false, true]);
    expect(log.liveIndex).toBe(1);
  });
});

describe("deriveActivityLog — append only", () => {
  it("ends a step at a call whose card renders after it", () => {
    const result = (id: string, toolName: string) =>
      entry({ id, kind: "tool_result", toolName, success: true });
    const log = deriveActivityLog(
      turnWith([
        result("tr-ask", "ask_user"),
        result("tr-schema", "get_block_schema"),
        result("tr-plan", "set_work_plan"),
        result("tr-nav", "navigate_browser"),
      ]),
    );
    expect(log.rows.map((row) => row.entries.map((e) => e.id))).toEqual([
      ["tr-ask"],
      ["tr-schema", "tr-plan"],
      ["tr-nav"],
    ]);
  });

  it("never rewrites or reorders a step once a later one exists", () => {
    const activity = laggingNarrationActivity();
    const shape = (k: number) =>
      deriveActivityLog(turnWith(activity.slice(0, k))).rows.map((row) => ({
        id: row.id,
        reason: row.reason,
        calls: row.entries.map(
          (e) => e.retryRootId ?? e.id.replace(/^t[cr]-/, ""),
        ),
      }));
    for (let k = 2; k <= activity.length; k += 1) {
      const before = shape(k - 1);
      expect(shape(k).slice(0, before.length - 1)).toEqual(before.slice(0, -1));
    }
    // "d" was spoken over a running call: it waits for that call to return,
    // then opens its own line rather than being dropped.
    const returned = activity.findIndex((e) => e.id === "tr-3");
    expect(shape(returned).map((row) => row.reason)).not.toContain("Reason d");
    expect(shape(activity.length).map((row) => row.reason)).toEqual([
      null,
      "Reason a",
      "Reason b",
      "Reason c",
      "Reason d",
      "Reason e",
      "Reason f",
      "Reason g",
    ]);
  });
});

describe("callRollup", () => {
  const call = (id: string, toolName: string, kind: ActivityEntry["kind"]) =>
    entry({ id, kind, toolName, success: true });

  it("names a step by its call kinds in first-appearance order, present tense only while one runs", () => {
    const settled = [
      call("tr-1", "navigate_browser", "tool_result"),
      call("tr-2", "fill_credential_field", "tool_result"),
      call("tr-3", "click", "tool_result"),
      call("tr-4", "set_work_plan", "tool_result"),
    ];
    expect(callRollup(settled)).toBe(
      "2 browser actions, used a saved login, updated its plan",
    );
    expect(
      callRollup([...settled, call("tc-5", "set_work_plan", "tool_call")]),
    ).toBe("2 browser actions, used a saved login, updating its plan");
    expect(
      callRollup([
        call("tr-1", "get_workflow_knowledge", "tool_result"),
        call("tr-2", "add_block", "tool_result"),
        call("tr-3", "edit_block", "tool_result"),
        call("tr-4", "run_blocks_and_collect_debug", "tool_result"),
      ]),
    ).toBe(
      "Looked up guidance, added 1 block, edited 1 block, tested the workflow",
    );
    expect(callRollup([entry({ id: "n-1", kind: "narration" })])).toBeNull();
  });

  it("claims only the writes that succeeded once a step finished", () => {
    const failed = (id: string, toolName: string) =>
      entry({ id, kind: "tool_result", toolName, success: false });
    expect(
      callRollup([
        failed("tr-1", "edit_block"),
        call("tr-2", "edit_block", "tool_result"),
      ]),
    ).toBe("Edited 1 block");
    expect(callRollup([failed("tr-1", "add_block")])).toBe(
      "Tried to add a block",
    );
    expect(callRollup([failed("tr-1", "fill_credential_field")])).toBe(
      "Tried a saved login",
    );
    expect(callRollup([failed("tr-1", "update_workflow")])).toBe(
      "Tried to update the workflow",
    );
    expect(
      callRollup([
        failed("tr-1", "click"),
        entry({
          id: "tr-2",
          kind: "tool_result",
          toolName: "click",
          success: true,
          retryRootId: "tr-1",
        }),
      ]),
    ).toBe("1 browser action");
  });

  it("reads every kind as finished once the turn settled, even with a call left unmatched", () => {
    const cancelled = [
      call("tr-1", "navigate_browser", "tool_result"),
      call("tc-2", "run_blocks_and_collect_debug", "tool_call"),
    ];
    expect(callRollup(cancelled)).toBe(
      "1 browser action, testing the workflow",
    );
    expect(callRollup(cancelled, true)).toBe(
      "1 browser action, tested the workflow",
    );
  });
});

describe("summarizeFinishedTurn", () => {
  const failedRun = (id: string, wrb: string) => ({
    activity: entry({
      id,
      kind: "tool_result",
      toolName: "run_blocks_and_collect_debug",
      success: false,
    }),
    block: block({
      workflowRunBlockId: wrb,
      label: "download_latest_pdf",
      state: "failed",
      startedAt: at(Number(id.replace(/\D/g, ""))),
    }),
  });

  it("counts failed tests, and says fixed only when the facts record a clean run", () => {
    const first = failedRun("tr-2", "wrb_a");
    const retry = entry({
      id: "tr-5",
      kind: "tool_result",
      toolName: "run_blocks_and_collect_debug",
      success: true,
    });
    const turn = turnWith(
      [
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "navigate_browser",
        }),
        first.activity,
        entry({ id: "tr-3", kind: "tool_result", toolName: "edit_block" }),
        retry,
      ],
      [first.block, block({ workflowRunBlockId: "wrb_b", startedAt: at(5) })],
    );
    const { rows } = deriveActivityLog(turn);

    const unconfirmed = summarizeFinishedTurn(rows, null);
    expect(unconfirmed).toMatchObject({
      steps: 4,
      failedTests: 1,
      fixed: false,
    });
    // The newest test passed, so nothing is named as still failing.
    expect(unconfirmed.stillFailing).toEqual([]);

    const fixed = summarizeFinishedTurn(rows, {
      factsAvailable: true,
      authoredBlockCount: 1,
      matchingSourceBlockCount: 1,
      evaluationState: "demonstrated",
      runId: "wr_1",
      runCompleted: true,
      terminalCause: null,
      blocksRunThisTurn: 1,
      ranCleanOnCurrentSource: true,
    });
    expect(fixed.fixed).toBe(true);
  });

  it("names the blocks the newest test left failing", () => {
    const first = failedRun("tr-1", "wrb_a");
    const second = failedRun("tr-3", "wrb_b");
    const { rows } = deriveActivityLog(
      turnWith(
        [
          first.activity,
          entry({ id: "tr-2", kind: "tool_result", toolName: "edit_block" }),
          second.activity,
        ],
        [first.block, second.block],
      ),
    );
    const summary = summarizeFinishedTurn(rows, null);
    expect(summary.failedTests).toBe(2);
    expect(summary.stillFailing.map((b) => b.workflowRunBlockId)).toEqual([
      "wrb_b",
    ]);
  });
});
