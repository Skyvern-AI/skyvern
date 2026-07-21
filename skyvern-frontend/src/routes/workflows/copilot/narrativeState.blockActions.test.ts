import { describe, expect, it } from "vitest";

import {
  CopilotBlockActionsEvent,
  EMPTY_NARRATIVE,
  RecordedActionSummary,
  TurnNarrativeState,
  applyNarrativeEvent,
  hydrateNarrativeFromPayload,
} from "./narrativeState";
import {
  WorkflowCopilotBlockProgressUpdate,
  WorkflowCopilotStreamResponseUpdate,
  WorkflowCopilotTurnStartUpdate,
} from "./workflowCopilotTypes";

const turnStart = (): WorkflowCopilotTurnStartUpdate => ({
  type: "turn_start",
  turn_id: "turn-1",
  turn_index: 0,
  mode: "build",
  timestamp: "2026-06-10T00:00:00Z",
});

const blockProgress = (
  overrides: Partial<WorkflowCopilotBlockProgressUpdate> &
    Pick<WorkflowCopilotBlockProgressUpdate, "block_label" | "status">,
): WorkflowCopilotBlockProgressUpdate => ({
  type: "block_progress",
  workflow_run_block_id: `wrb_${overrides.block_label}`,
  block_type: "code",
  iteration: 0,
  timestamp: "2026-06-10T00:00:04Z",
  ...overrides,
});

const recordedAction = (
  overrides: Partial<RecordedActionSummary> &
    Pick<RecordedActionSummary, "actionId">,
): RecordedActionSummary => ({
  label: "Click",
  summary: null,
  durationMs: 200,
  failed: false,
  ...overrides,
});

const blockActions = (
  overrides: Partial<CopilotBlockActionsEvent> &
    Pick<CopilotBlockActionsEvent, "blocks">,
): CopilotBlockActionsEvent => ({
  type: "client_block_actions",
  receivedAtMs: 1_000,
  ...overrides,
});

function reduce(events: Parameters<typeof applyNarrativeEvent>[1][]) {
  return events.reduce(
    (state: TurnNarrativeState, event) => applyNarrativeEvent(state, event),
    EMPTY_NARRATIVE,
  );
}

const oneBlockRunning = [
  turnStart(),
  blockProgress({ block_label: "open_search", status: "running" }),
  blockProgress({ block_label: "open_search", status: "completed" }),
];

const twoBlocksRunning = [
  turnStart(),
  blockProgress({ block_label: "open_search", status: "running" }),
  blockProgress({ block_label: "open_search", status: "completed" }),
  blockProgress({ block_label: "search_person", status: "running" }),
  blockProgress({ block_label: "search_person", status: "completed" }),
];

describe("applyNarrativeEvent — client_block_actions", () => {
  it("merges recorded actions into the matched block", () => {
    const s = reduce([
      ...oneBlockRunning,
      blockActions({
        blocks: [
          {
            workflowRunBlockId: "wrb_open_search",
            actions: [recordedAction({ actionId: "a1" })],
          },
        ],
      }),
    ]);
    const block = s.blocks.find((b) => b.label === "open_search")!;
    expect(block.recordedActions).toHaveLength(1);
    expect(block.recordedActions![0]!.actionId).toBe("a1");
    expect(block.recordedActionsAt).toBe(1_000);
  });

  it("grows the action list when a later live-poll fetch returns new actions", () => {
    // First poll sees one action; a later poll returns the growing set. The new
    // action appends (keyed by actionId) and the reveal anchor stays put so
    // already-revealed rows don't restart. Fails on the old freeze-once reducer,
    // which dropped everything after the first application.
    const first = blockActions({
      blocks: [
        {
          workflowRunBlockId: "wrb_open_search",
          actions: [recordedAction({ actionId: "a1" })],
        },
      ],
    });
    const second = blockActions({
      receivedAtMs: 99_999,
      blocks: [
        {
          workflowRunBlockId: "wrb_open_search",
          actions: [
            recordedAction({ actionId: "a1" }),
            recordedAction({ actionId: "a2" }),
          ],
        },
      ],
    });
    const s = reduce([...oneBlockRunning, first, second]);
    const block = s.blocks.find((b) => b.label === "open_search")!;
    expect(block.recordedActions!.map((a) => a.actionId)).toEqual(["a1", "a2"]);
    expect(block.recordedActionsAt).toBe(1_000);
  });

  it("is idempotent — a re-fetch returning already-seen actions is a no-op", () => {
    const first = blockActions({
      blocks: [
        {
          workflowRunBlockId: "wrb_open_search",
          actions: [recordedAction({ actionId: "a1" })],
        },
      ],
    });
    const second = blockActions({
      receivedAtMs: 99_999,
      blocks: [
        {
          workflowRunBlockId: "wrb_open_search",
          actions: [recordedAction({ actionId: "a1" })],
        },
      ],
    });
    const withFirst = reduce([...oneBlockRunning, first]);
    const after = applyNarrativeEvent(withFirst, second);
    // No new actionId -> same state reference, so no re-render and no duplicate row.
    expect(after).toBe(withFirst);
    const block = after.blocks.find((b) => b.label === "open_search")!;
    expect(block.recordedActions).toHaveLength(1);
    expect(block.recordedActionsAt).toBe(1_000);
  });

  it("ignores an entry whose workflow_run_block_id matches no known block", () => {
    const s = reduce([
      ...oneBlockRunning,
      blockActions({
        blocks: [
          {
            workflowRunBlockId: "wrb_does_not_exist",
            actions: [recordedAction({ actionId: "a1" })],
          },
        ],
      }),
    ]);
    const block = s.blocks.find((b) => b.label === "open_search")!;
    expect(block.recordedActions).toBeUndefined();
  });

  it("ignores an entry with an empty actions array", () => {
    const s = reduce([
      ...oneBlockRunning,
      blockActions({
        blocks: [{ workflowRunBlockId: "wrb_open_search", actions: [] }],
      }),
    ]);
    const block = s.blocks.find((b) => b.label === "open_search")!;
    expect(block.recordedActions).toBeUndefined();
  });

  it("returns the same state reference when nothing matches, so a message from another turn/run doesn't re-render", () => {
    const before = reduce(oneBlockRunning);
    const after = applyNarrativeEvent(
      before,
      blockActions({
        blocks: [{ workflowRunBlockId: "wrb_does_not_exist", actions: [] }],
      }),
    );
    // WorkflowCopilotChat.tsx's per-message patch on fetch arrival relies on
    // this identity to skip messages the event doesn't touch.
    expect(after).toBe(before);
  });

  it("preserves recordedActions across a late/replayed block_progress for the same block", () => {
    const withActions = reduce([
      ...oneBlockRunning,
      blockActions({
        blocks: [
          {
            workflowRunBlockId: "wrb_open_search",
            actions: [recordedAction({ actionId: "a1" })],
          },
        ],
      }),
    ]);
    const replayed = applyNarrativeEvent(
      withActions,
      blockProgress({ block_label: "open_search", status: "completed" }),
    );
    const block = replayed.blocks.find((b) => b.label === "open_search")!;
    expect(block.recordedActions).toHaveLength(1);
    expect(block.recordedActionsAt).toBe(1_000);
  });

  it("carries recordedActions through a response event that hydrates from a real narrative_payload", () => {
    const withActions = reduce([
      ...oneBlockRunning,
      blockActions({
        blocks: [
          {
            workflowRunBlockId: "wrb_open_search",
            actions: [recordedAction({ actionId: "a1" })],
          },
        ],
      }),
    ]);
    const responseEvent: WorkflowCopilotStreamResponseUpdate = {
      type: "response",
      workflow_copilot_chat_id: "chat_1",
      message: "Done",
      response_time: "2026-06-10T00:01:00Z",
      proposal_disposition: "no_proposal",
      turn_id: "turn-1",
      narrative_payload: {
        turnId: "turn-1",
        turnIndex: 0,
        mode: "build",
        terminal: "response",
        blocks: [
          {
            workflowRunBlockId: "wrb_open_search",
            label: "open_search",
            blockType: "code",
            state: "completed",
            lastSeenIteration: 0,
            activity: [],
            startedAt: "2026-06-10T00:00:04Z",
            endedAt: "2026-06-10T00:01:00Z",
          },
        ],
      },
    };
    const after = applyNarrativeEvent(withActions, responseEvent);
    const block = after.blocks.find((b) => b.label === "open_search")!;
    expect(block.recordedActions).toHaveLength(1);
    expect(block.recordedActionsAt).toBe(1_000);
  });

  it("staggers a second block's reveal start past the first block's own schedule total", () => {
    const s = reduce([
      ...twoBlocksRunning,
      blockActions({
        receivedAtMs: 5_000,
        blocks: [
          {
            workflowRunBlockId: "wrb_open_search",
            actions: [recordedAction({ actionId: "a1", durationMs: 200 })],
          },
          {
            workflowRunBlockId: "wrb_search_person",
            actions: [recordedAction({ actionId: "a2", durationMs: 300 })],
          },
        ],
      }),
    ]);
    const first = s.blocks.find((b) => b.label === "open_search")!;
    const second = s.blocks.find((b) => b.label === "search_person")!;
    expect(first.recordedActionsAt).toBe(5_000);
    // second block starts after the first block's own (clamped) 200ms total
    expect(second.recordedActionsAt).toBe(5_200);
  });
});

describe("hydrateNarrativeFromPayload — client_block_actions fields", () => {
  it("never sets recordedActions from a BE-built history payload", () => {
    const hydrated = hydrateNarrativeFromPayload({
      turnId: "turn-1",
      turnIndex: 0,
      mode: "build",
      terminal: "response",
      blocks: [
        {
          label: "open_search",
          blockType: "code",
          state: "completed",
          lastSeenIteration: 0,
          activity: [],
          startedAt: "2026-06-10T00:00:04Z",
          endedAt: "2026-06-10T00:01:00Z",
        },
      ],
    })!;
    expect(hydrated.blocks[0]!.recordedActions).toBeUndefined();
    expect(hydrated.blocks[0]!.recordedActionsAt).toBeUndefined();
  });
});
