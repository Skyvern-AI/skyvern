import { describe, expect, it } from "vitest";

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

describe("activity timestamp parity across hydration", () => {
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

  it("hydrates a payload whose entries and blocks lack the new keys", () => {
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
    expect(hydrated!.blocks[0]!.workflowRunBlockId).toBe("");
    expect(groupedDuration(hydrated!.designActivity)).toBeNull();
  });
});
