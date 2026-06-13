import { describe, expect, it } from "vitest";

import {
  EMPTY_NARRATIVE,
  TurnNarrativeState,
  applyNarrativeEvent,
  hydrateNarrativeFromPayload,
  isBlockOk,
} from "./narrativeState";
import {
  WorkflowCopilotBlockProgressUpdate,
  WorkflowCopilotRunOutcomeUpdate,
  WorkflowCopilotStreamErrorUpdate,
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

const runOutcome = (
  overrides: Partial<WorkflowCopilotRunOutcomeUpdate> &
    Pick<WorkflowCopilotRunOutcomeUpdate, "verdict">,
): WorkflowCopilotRunOutcomeUpdate => ({
  type: "run_outcome",
  workflow_run_id: "wr_1",
  workflow_run_block_ids: ["wrb_open_search", "wrb_search_person"],
  block_labels: ["open_search", "search_person"],
  reason_code: null,
  display_reason: null,
  iteration: 0,
  timestamp: "2026-06-10T00:01:00Z",
  ...overrides,
});

const response = (): WorkflowCopilotStreamResponseUpdate => ({
  type: "response",
  workflow_copilot_chat_id: "chat-1",
  message: "Done.",
  response_time: "2026-06-10T00:02:00Z",
  proposal_disposition: "auto_applicable",
});

const errorUpdate = (): WorkflowCopilotStreamErrorUpdate => ({
  type: "error",
  error: "Something broke.",
});

function reduce(events: Parameters<typeof applyNarrativeEvent>[1][]) {
  return events.reduce(
    (state: TurnNarrativeState, event) => applyNarrativeEvent(state, event),
    EMPTY_NARRATIVE,
  );
}

const bothBlocksRan = [
  turnStart(),
  blockProgress({ block_label: "open_search", status: "running" }),
  blockProgress({ block_label: "open_search", status: "completed" }),
  blockProgress({ block_label: "search_person", status: "running" }),
  blockProgress({ block_label: "search_person", status: "completed" }),
];

describe("applyNarrativeEvent — run_outcome", () => {
  it("negative verdict withholds the success affordance from completed rows", () => {
    const s = reduce([
      ...bothBlocksRan,
      runOutcome({ verdict: "evaluating" }),
      runOutcome({
        verdict: "not_demonstrated",
        reason_code: "blocker_reported",
        display_reason: "The search stayed gated by a verification challenge.",
      }),
    ]);
    expect(s.blocks).toHaveLength(2);
    for (const b of s.blocks) {
      expect(b.state).toBe("completed");
      expect(b.outcome).toBe("not_demonstrated");
      expect(b.outcomeReason).toBe(
        "The search stayed gated by a verification challenge.",
      );
      expect(isBlockOk(b)).toBe(false);
    }
  });

  it("evaluating hold withholds the success affordance until the final frame", () => {
    const s = reduce([...bothBlocksRan, runOutcome({ verdict: "evaluating" })]);
    for (const b of s.blocks) {
      expect(b.outcome).toBe("evaluating");
      expect(isBlockOk(b)).toBe(false);
    }
  });

  it("response-terminal sweep cannot resurrect success on a negative-verdict row", () => {
    const s = reduce([
      turnStart(),
      blockProgress({ block_label: "open_search", status: "running" }),
      blockProgress({ block_label: "open_search", status: "completed" }),
      // search_person never gets its terminal block_progress.
      blockProgress({ block_label: "search_person", status: "running" }),
      runOutcome({ verdict: "evaluating" }),
      runOutcome({
        verdict: "not_demonstrated",
        reason_code: "blocker_reported",
      }),
      response(),
    ]);
    const swept = s.blocks.find((b) => b.label === "search_person")!;
    expect(swept.state).toBe("completed");
    expect(swept.outcome).toBe("not_demonstrated");
    expect(isBlockOk(swept)).toBe(false);
    expect(s.blocks.some((b) => isBlockOk(b))).toBe(false);
  });

  it("a row stuck in evaluating at terminal never satisfies isBlockOk", () => {
    const s = reduce([
      ...bothBlocksRan,
      runOutcome({ verdict: "evaluating" }),
      response(),
    ]);
    for (const b of s.blocks) {
      expect(b.state).toBe("completed");
      expect(b.outcome).toBe("evaluating");
      expect(isBlockOk(b)).toBe(false);
    }
  });

  it("error-terminal sweep changes lifecycle state only, never the verdict", () => {
    const s = reduce([
      turnStart(),
      blockProgress({ block_label: "search_person", status: "running" }),
      runOutcome({
        verdict: "not_demonstrated",
        workflow_run_block_ids: ["wrb_search_person"],
        block_labels: ["search_person"],
      }),
      errorUpdate(),
    ]);
    const b = s.blocks[0]!;
    expect(b.state).toBe("failed");
    expect(b.outcome).toBe("not_demonstrated");
    expect(isBlockOk(b)).toBe(false);
  });

  it("a late block_progress upsert cannot wipe a recorded verdict", () => {
    const s = reduce([
      ...bothBlocksRan,
      runOutcome({ verdict: "evaluating" }),
      runOutcome({
        verdict: "not_demonstrated",
        reason_code: "blocker_reported",
        display_reason: "The search stayed gated by a verification challenge.",
      }),
      blockProgress({
        block_label: "search_person",
        status: "completed",
        timestamp: "2026-06-10T00:01:30Z",
      }),
    ]);
    const late = s.blocks.find((b) => b.label === "search_person")!;
    expect(late.state).toBe("completed");
    expect(late.outcome).toBe("not_demonstrated");
    expect(late.outcomeReason).toBe(
      "The search stayed gated by a verification challenge.",
    );
    expect(isBlockOk(late)).toBe(false);
  });

  it("applies by run-block id only; other rows keep their own verdict", () => {
    const s = reduce([
      ...bothBlocksRan,
      runOutcome({
        verdict: "not_demonstrated",
        workflow_run_block_ids: ["wrb_search_person"],
        block_labels: ["search_person"],
      }),
      runOutcome({
        verdict: "demonstrated",
        workflow_run_id: "wr_2",
        workflow_run_block_ids: ["wrb_open_search"],
        block_labels: ["open_search"],
      }),
    ]);
    const open = s.blocks.find((b) => b.label === "open_search")!;
    const search = s.blocks.find((b) => b.label === "search_person")!;
    expect(open.outcome).toBe("demonstrated");
    expect(isBlockOk(open)).toBe(true);
    expect(search.outcome).toBe("not_demonstrated");
    expect(isBlockOk(search)).toBe(false);
  });

  it("demonstrated and not_evaluated verdicts keep the success affordance", () => {
    for (const verdict of ["demonstrated", "not_evaluated"] as const) {
      const s = reduce([
        ...bothBlocksRan,
        runOutcome({ verdict: "evaluating" }),
        runOutcome({ verdict }),
        response(),
      ]);
      for (const b of s.blocks) {
        expect(b.outcome).toBe(verdict);
        expect(isBlockOk(b)).toBe(true);
      }
    }
  });

  it("without run_outcome frames (old backend) rendering state is unchanged", () => {
    const s = reduce([...bothBlocksRan, response()]);
    for (const b of s.blocks) {
      expect(b.outcome).toBeUndefined();
      expect(b.outcomeReason).toBeUndefined();
      expect(isBlockOk(b)).toBe(true);
    }
  });
});

describe("hydrateNarrativeFromPayload — outcome", () => {
  const payloadBlock = (overrides: Record<string, unknown>) => ({
    label: "search_person",
    blockType: "code",
    state: "completed",
    lastSeenIteration: 0,
    activity: [],
    startedAt: "2026-06-10T00:00:04Z",
    endedAt: "2026-06-10T00:01:00Z",
    ...overrides,
  });

  const payload = (blocks: Record<string, unknown>[]) => ({
    turnId: "turn-1",
    turnIndex: 0,
    mode: "build",
    terminal: "response",
    terminalMessage: "Done.",
    startedAt: "2026-06-10T00:00:00Z",
    endedAt: "2026-06-10T00:02:00Z",
    blocks,
  });

  it("round-trips outcome/outcomeReason so reload renders like the live stream", () => {
    const live = reduce([
      turnStart(),
      blockProgress({ block_label: "search_person", status: "running" }),
      blockProgress({ block_label: "search_person", status: "completed" }),
      runOutcome({
        verdict: "not_demonstrated",
        workflow_run_block_ids: ["wrb_search_person"],
        block_labels: ["search_person"],
        reason_code: "blocker_reported",
        display_reason: "The search stayed gated by a verification challenge.",
      }),
      response(),
    ]);
    const liveRow = live.blocks[0]!;

    const hydrated = hydrateNarrativeFromPayload(
      payload([
        payloadBlock({
          outcome: "not_demonstrated",
          outcomeReason: "The search stayed gated by a verification challenge.",
        }),
      ]),
    )!;
    const hydratedRow = hydrated.blocks[0]!;

    expect(hydratedRow.state).toBe(liveRow.state);
    expect(hydratedRow.outcome).toBe(liveRow.outcome);
    expect(hydratedRow.outcomeReason).toBe(liveRow.outcomeReason);
    expect(isBlockOk(hydratedRow)).toBe(false);
    expect(isBlockOk(liveRow)).toBe(false);
  });

  it("hydrates rows without outcome keys exactly as before", () => {
    const hydrated = hydrateNarrativeFromPayload(payload([payloadBlock({})]))!;
    const row = hydrated.blocks[0]!;
    expect(row.outcome).toBeUndefined();
    expect(row.outcomeReason).toBeUndefined();
    expect(isBlockOk(row)).toBe(true);
  });

  it("ignores unknown outcome values", () => {
    const hydrated = hydrateNarrativeFromPayload(
      payload([payloadBlock({ outcome: "maybe", outcomeReason: 7 })]),
    )!;
    const row = hydrated.blocks[0]!;
    expect(row.outcome).toBeUndefined();
    expect(row.outcomeReason).toBeUndefined();
    expect(isBlockOk(row)).toBe(true);
  });

  it("hydrate sweep promotes a stuck-running row without inventing a verdict", () => {
    const hydrated = hydrateNarrativeFromPayload(
      payload([
        payloadBlock({
          state: "running",
          outcome: "not_demonstrated",
          endedAt: null,
        }),
      ]),
    )!;
    const row = hydrated.blocks[0]!;
    expect(row.state).toBe("completed");
    expect(row.outcome).toBe("not_demonstrated");
    expect(isBlockOk(row)).toBe(false);
  });
});
