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

    expect(log.rows.map((row) => row.reason)).toEqual([
      null,
      "Opening the search form",
      null,
      "Restoring the results page",
    ]);
    expect(log.rows[0]?.entries[0]?.success).toBe(true);
    expect(log.rows[2]?.entries[0]?.success).toBe(false);
    expect(log.rows[1]?.entries).toEqual([]);
    expect(log.rows[3]?.entries).toEqual([]);
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
    expect(log.rows.map((row) => row.kind)).toEqual(["run", "author"]);
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

    expect(labelsPerRow(log)).toEqual([[], ["run_b_block"]]);
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
      codegenProgress: { blockLabels: ["open_page"], generationId: null },
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
        result("tr-delete", "delete_saved_credentials"),
        result("tr-list", "list_credentials"),
      ]),
    );
    expect(log.rows.map((row) => row.entries.map((e) => e.id))).toEqual([
      ["tr-ask"],
      ["tr-schema", "tr-plan"],
      ["tr-nav", "tr-delete"],
      ["tr-list"],
    ]);
  });

  it("never rewrites or reorders a step once a later one exists", () => {
    const activity = laggingNarrationActivity();
    const payload = {
      turnId: "legacy",
      turnIndex: 0,
      terminal: "response",
      blocks: [],
      designActivity: activity,
    };
    const live = turnWith(activity);
    const terminal = applyNarrativeEvent(live, terminalResponse(payload));
    const reopened = hydrateNarrativeFromPayload(payload)!;
    for (const phase of [live, terminal, reopened]) {
      const rows = deriveActivityLog(phase).rows;
      expect(
        rows.filter((row) => row.reason !== null).map((row) => row.reason),
      ).toEqual(
        activity
          .filter((entry) => entry.kind === "narration")
          .map((entry) => entry.text),
      );
      expect(
        rows
          .filter((row) => row.reason !== null)
          .every((row) => row.entries.length === 0),
      ).toBe(true);
      expect(
        rows
          .filter((row) => row.entries.length > 0)
          .every((row) => row.reason === null),
      ).toBe(true);
    }
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
    expect(
      callRollup([call("tr-1", "delete_saved_credentials", "tool_result")]),
    ).toBe("1 other step");
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

describe("actor reason ownership", () => {
  const call = (id: string, reason: string | null, second: number) => ({
    type: "tool_call" as const,
    tool_name: "evaluate",
    tool_call_id: id,
    tool_input: { expression: id },
    iteration: 0,
    reason,
    timestamp: at(second),
    activity_bucket: { kind: "design" as const },
  });
  const receipt = (id: string, success: boolean, second: number) => ({
    type: "tool_result" as const,
    tool_name: "evaluate",
    tool_call_id: id,
    summary: success ? "Read page" : "Page unavailable",
    success,
    iteration: 0,
    reason: "A late replacement must never change shown text",
    timestamp: at(second),
    activity_bucket: { kind: "design" as const },
  });
  it("keeps concurrent explained calls separate in initial order across reversed results and all phases", () => {
    let turn: TurnNarrativeState = {
      ...EMPTY_NARRATIVE,
      turnId: "actor-turn",
      turnIndex: 0,
    };
    turn = applyNarrativeEvent(
      turn,
      call("a", "I will inspect availability.", 1),
    );
    turn = applyNarrativeEvent(turn, call("b", "The booking succeeded.", 2));
    const expected = ["I will inspect availability.", "The booking succeeded."];
    expect(deriveActivityLog(turn).rows.map((row) => row.reason)).toEqual(
      expected,
    );
    turn = applyNarrativeEvent(turn, receipt("b", false, 4));
    turn = applyNarrativeEvent(turn, receipt("a", true, 5));
    const rows = deriveActivityLog(turn).rows;
    expect(rows.map((row) => row.id)).toEqual(["a", "b"]);
    expect(rows.map((row) => row.reason)).toEqual(expected);
    expect(rows[1]!.entries[0]!.success).toBe(false);
    const payload = {
      turnId: turn.turnId,
      turnIndex: 0,
      terminal: "response",
      designActivity: turn.designActivity,
      blocks: [],
      startedAt: at(0),
      endedAt: at(6),
    };
    const terminal = applyNarrativeEvent(turn, terminalResponse(payload));
    const reopened = hydrateNarrativeFromPayload(payload)!;
    for (const phase of [terminal, reopened]) {
      expect(
        deriveActivityLog(phase).rows.map((row) => [row.id, row.reason]),
      ).toEqual(rows.map((row) => [row.id, row.reason]));
    }
  });
  it("pins all tools to their original run block through transitions and eviction", () => {
    let turn = turnWith(
      [],
      [block({ workflowRunBlockId: "wrb_a", state: "running" })],
    );
    turn = applyNarrativeEvent(turn, {
      ...call("owned", "I will inspect this running step.", 1),
      activity_bucket: { kind: "block", workflow_run_block_id: "wrb_a" },
    });
    for (let i = 0; i < 40; i++)
      turn = applyNarrativeEvent(turn, {
        ...call(`filler-${i}`, null, 2),
        activity_bucket: { kind: "block", workflow_run_block_id: "wrb_a" },
      });
    turn = {
      ...turn,
      blocks: [
        ...turn.blocks.map((b) => ({ ...b, state: "completed" as const })),
        block({ workflowRunBlockId: "wrb_b", state: "running" }),
      ],
    };
    turn = applyNarrativeEvent(turn, {
      ...receipt("owned", true, 8),
      activity_bucket: undefined,
    });
    expect(
      turn.blocks[0]!.activity[turn.blocks[0]!.activity.length - 1]?.id,
    ).toBe("tr-owned");
    expect(
      turn.blocks[0]!.activity[turn.blocks[0]!.activity.length - 1]?.reason,
    ).toBe("I will inspect this running step.");
    expect(
      turn.blocks[0]!.activity[turn.blocks[0]!.activity.length - 1]
        ?.activityStartedAt,
    ).toBe(at(1));
    expect(turn.blocks[1]!.activity).toEqual([]);
    turn = applyNarrativeEvent(turn, {
      ...receipt("unseen", true, 9),
      activity_bucket: undefined,
    });
    expect(turn.designActivity[turn.designActivity.length - 1]?.id).toBe(
      "tr-unseen",
    );
  });
  it("keeps failed and successful explained retries separate and unowned legacy text standalone", () => {
    const legacy = entry({
      id: "n-lag",
      kind: "narration",
      text: "An old delayed explanation",
      iteration: 0,
      timestamp: at(3),
    });
    const entries = [
      entry({
        id: "tc-failed",
        kind: "tool_call",
        toolName: "evaluate",
        reason: "Inspect the first page",
        timestamp: at(1),
      }),
      entry({
        id: "tr-failed",
        kind: "tool_result",
        toolName: "evaluate",
        success: false,
        text: "Timeout",
        reason: "Inspect the first page",
        timestamp: at(2),
      }),
      legacy,
      entry({
        id: "tc-retry",
        kind: "tool_call",
        toolName: "evaluate",
        reason: "Inspect the refreshed page",
        timestamp: at(4),
      }),
      entry({
        id: "tr-retry",
        kind: "tool_result",
        toolName: "evaluate",
        success: true,
        text: "Read refreshed page",
        reason: "Inspect the refreshed page",
        timestamp: at(5),
      }),
    ];
    const rows = deriveActivityLog(turnWith(entries)).rows;
    expect(rows.map((row) => row.reason)).toEqual([
      "Inspect the first page",
      legacy.text,
      "Inspect the refreshed page",
    ]);
    expect(rows[0]!.entries[0]!.success).toBe(false);
    expect(rows[1]!.entries).toEqual([]);
    expect(rows[2]!.entries[0]!.success).toBe(true);
  });
});
