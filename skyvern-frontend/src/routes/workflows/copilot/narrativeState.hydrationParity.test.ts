import { describe, expect, it } from "vitest";

import {
  callRollup,
  deriveActivityLog,
  failedRowBlocks,
  passedBlockCount,
  summarizeFinishedTurn,
} from "./copilotActivityLog";
import capture from "./narrativeState.hydrationParity.fixture.json";
import {
  ActivityEntry,
  EMPTY_NARRATIVE,
  NarrativeEvent,
  TurnNarrativeState,
  applyNarrativeEvent,
  formatElapsed,
  hydrateNarrativeFromPayload,
} from "./narrativeState";

// Produced by dev_scripts/replay_narrative_timestamp_parity.py driving the real
// streaming_adapter emit path, so neither leg here is a hand-written shape.
const liveUpdates = capture.updates as unknown as NarrativeEvent[];
const persistedPayload = capture.payload as unknown as Record<string, unknown>;

function groupedDuration(entries: ActivityEntry[]): string | null {
  const stamps = entries
    .map((entry) => entry.timestamp)
    .filter((stamp): stamp is string => typeof stamp === "string");
  if (stamps.length === 0) return null;
  return formatElapsed(stamps[0]!, stamps[stamps.length - 1]!);
}

// A narration entry's id embeds its own ISO timestamp, and the live and persisted
// sides serialize UTC differently ("Z" versus "+00:00"), so pair those by iteration.
function pairKey(entry: ActivityEntry): string {
  return entry.kind === "narration" ? `n-${entry.iteration}` : entry.id;
}

function replayLive(): TurnNarrativeState {
  return liveUpdates.reduce<TurnNarrativeState>(
    (state, event) => applyNarrativeEvent(state, event),
    { ...EMPTY_NARRATIVE },
  );
}

// Everything a collapsed step line and the folded turn show.
function logLines(
  turn: TurnNarrativeState,
  facts: TurnNarrativeState["turnFacts"],
) {
  const { rows } = deriveActivityLog(turn);
  const fold = summarizeFinishedTurn(rows, facts);
  return {
    rows: rows.map((row) => ({
      id: row.id,
      kind: row.kind,
      reason: row.reason,
      rollup: callRollup(row.entries),
      failed: failedRowBlocks(row).map((b) => b.workflowRunBlockId),
      passed: passedBlockCount(row),
      diff: row.codeDiffs.map((d) => [d.label, d.added, d.removed]),
    })),
    fold: {
      ...fold,
      stillFailing: fold.stillFailing.map((b) => b.workflowRunBlockId),
    },
  };
}

describe("activity log parity across hydration", () => {
  it("renders the same step lines, failure pins and reasons live and after a reload", () => {
    const hydrated = hydrateNarrativeFromPayload(persistedPayload)!;
    const live = logLines(replayLive(), hydrated.turnFacts);

    expect(logLines(hydrated, hydrated.turnFacts)).toEqual(live);
    // The capture exercises each piece, so equality is not vacuous.
    expect(live.rows.some((row) => row.failed.includes("wrb_first"))).toBe(
      true,
    );
    // The capture's retried block ran but was not evaluated, so no row claims it passed.
    expect(live.rows.some((row) => row.kind === "run")).toBe(true);
    expect(live.rows.every((row) => row.passed === null)).toBe(true);
    expect(live.rows.some((row) => row.reason !== null)).toBe(true);
    expect(live.fold.failedTests).toBe(1);
  });
});

describe("activity timestamp parity across hydration", () => {
  it("preserves observed fail-edit-retry attempts across terminal replacement and hydration", () => {
    const live = replayLive();
    const hydrated = hydrateNarrativeFromPayload(persistedPayload)!;
    const terminal = applyNarrativeEvent(live, {
      type: "response",
      workflow_copilot_chat_id: "chat-replay",
      message: "done",
      response_time: "2026-09-08T00:00:10Z",
      proposal_disposition: "review_tested",
      turn_id: "turn-replay",
      narrative_payload: persistedPayload,
    });

    expect(live.draft?.blockCount).toBe(20);
    expect(live.blocks.map((block) => block.workflowRunBlockId)).toEqual([
      "wrb_first",
      "wrb_retry",
    ]);
    expect(live.blocks.map((block) => block.label)).toEqual([
      "block_19",
      "block_19",
    ]);
    expect(live.blocks.map((block) => block.outcome)).toEqual([
      "not_demonstrated",
      "not_evaluated",
    ]);
    expect(terminal.blocks).toEqual(hydrated.blocks);
  });

  it("stamps every persisted entry with the clock read its live update carried", () => {
    const live = replayLive();
    const hydrated = hydrateNarrativeFromPayload(persistedPayload);
    expect(hydrated).not.toBeNull();

    const liveById = new Map(live.designActivity.map((e) => [pairKey(e), e]));
    expect(hydrated!.designActivity.length).toBeGreaterThan(0);
    for (const entry of hydrated!.designActivity) {
      expect(entry.timestamp).toBeTypeOf("string");
      const twin = liveById.get(pairKey(entry));
      expect(twin, `no live entry for ${entry.id}`).toBeDefined();
      expect(Date.parse(entry.timestamp!)).toBe(Date.parse(twin!.timestamp!));
    }
  });

  it("renders the same grouped duration live and after a hydrated reload", () => {
    const liveDuration = groupedDuration(replayLive().designActivity);
    const hydratedDuration = groupedDuration(
      hydrateNarrativeFromPayload(persistedPayload)!.designActivity,
    );
    expect(liveDuration).not.toBeNull();
    expect(hydratedDuration).toBe(liveDuration);
  });

  it("hydrates legacy activity without inventing an evidence-free block", () => {
    const olderBackendPayload = {
      turnId: "turn-old",
      turnIndex: 0,
      designStarted: true,
      designEnded: true,
      draft: null,
      blocks: [
        {
          label: "log_in",
          blockType: "task",
          state: "completed",
          lastSeenIteration: 0,
          activity: [],
          startedAt: null,
          endedAt: null,
        },
      ],
      terminal: "response",
      terminalMessage: "done",
      narrativeSummary: null,
      priorBlockCount: null,
      designActivity: [
        {
          kind: "tool_call",
          text: "Updating workflow…",
          iteration: 0,
          toolName: "update_workflow",
          displayLabel: "Updating workflow",
          id: "tc-c1",
        },
      ],
      startedAt: null,
      endedAt: null,
    };

    const hydrated = hydrateNarrativeFromPayload(olderBackendPayload);

    expect(hydrated).not.toBeNull();
    expect(hydrated!.designActivity).toHaveLength(1);
    expect(hydrated!.designActivity[0]!.timestamp).toBeUndefined();
    expect(hydrated!.blocks).toEqual([]);
    expect(groupedDuration(hydrated!.designActivity)).toBeNull();
  });
});
