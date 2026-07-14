import { describe, expect, it } from "vitest";

import {
  AUTHORING_TOOLS,
  RUN_TOOLS,
  derivePhases,
  shouldArmDraftingGapTimer,
  showPhaseChecklist,
} from "./copilotPhases";
import {
  ActivityEntry,
  BlockState,
  EMPTY_NARRATIVE,
  TurnNarrativeState,
} from "./narrativeState";

const entry = (
  overrides: Partial<ActivityEntry> & Pick<ActivityEntry, "id" | "kind">,
): ActivityEntry => ({
  text: "did something",
  iteration: 0,
  ...overrides,
});

const block = (overrides: Partial<BlockState> = {}): BlockState => ({
  workflowRunBlockId: "wrb_1",
  label: "block_1",
  blockType: "task",
  state: "completed",
  lastSeenIteration: 0,
  activity: [],
  startedAt: "2026-06-10T00:00:00Z",
  endedAt: "2026-06-10T00:00:10Z",
  ...overrides,
});

const turn = (
  overrides: Partial<TurnNarrativeState> = {},
): TurnNarrativeState => ({
  ...EMPTY_NARRATIVE,
  turnId: "turn-1",
  turnIndex: 0,
  mode: "build",
  designStarted: true,
  ...overrides,
});

function phase(rows: ReturnType<typeof derivePhases>, id: string) {
  return rows.find((r) => r.id === id)!;
}

describe("AUTHORING_TOOLS / RUN_TOOLS", () => {
  it("update_workflow is authoring-only, not a run tool (Codex catch)", () => {
    expect(AUTHORING_TOOLS.has("update_workflow")).toBe(true);
    expect(RUN_TOOLS.has("update_workflow")).toBe(false);
  });

  it("update_and_run_blocks is both authoring and a run tool", () => {
    expect(AUTHORING_TOOLS.has("update_and_run_blocks")).toBe(true);
    expect(RUN_TOOLS.has("update_and_run_blocks")).toBe(true);
  });

  it("run_blocks_and_collect_debug is a run tool only, not authoring", () => {
    expect(RUN_TOOLS.has("run_blocks_and_collect_debug")).toBe(true);
    expect(AUTHORING_TOOLS.has("run_blocks_and_collect_debug")).toBe(false);
  });
});

describe("derivePhases — bucket split keeps update_workflow in Draft (Codex catch)", () => {
  it("an update_workflow tool_call on a draft-only turn lands in the Draft bucket, not Test", () => {
    const t = turn({
      designEnded: true,
      designActivity: [
        entry({ id: "1", kind: "tool_call", toolName: "navigate_browser" }),
        entry({ id: "2", kind: "tool_call", toolName: "update_workflow" }),
      ],
    });
    const rows = derivePhases(t);
    expect(phase(rows, "draft").entries.map((e) => e.id)).toEqual(["2"]);
    expect(phase(rows, "test").entries).toEqual([]);
  });
});

describe("derivePhases — bucket split", () => {
  it("routes pre-authoring activity to explore, RUN_TOOLS to test, post-authoring narration to draft", () => {
    const t = turn({
      designActivity: [
        entry({ id: "1", kind: "tool_call", toolName: "navigate_browser" }),
        entry({ id: "2", kind: "narration" }),
        entry({
          id: "3",
          kind: "tool_call",
          toolName: "update_and_run_blocks",
        }),
        entry({ id: "4", kind: "narration" }),
      ],
    });
    const rows = derivePhases(t);
    expect(phase(rows, "explore").entries.map((e) => e.id)).toEqual(["1", "2"]);
    expect(phase(rows, "test").entries.map((e) => e.id)).toEqual(["3"]);
    expect(phase(rows, "draft").entries.map((e) => e.id)).toEqual(["4"]);
  });
});

describe("derivePhases — live progression", () => {
  it("explore is active before any authoring activity", () => {
    const t = turn({
      designActivity: [
        entry({ id: "1", kind: "tool_call", toolName: "navigate_browser" }),
      ],
    });
    expect(phase(derivePhases(t), "explore").status).toBe("active");
    expect(phase(derivePhases(t), "draft").status).toBe("pending");
  });

  it("draft goes active once the drafting-gap client hint fires, before any block runs", () => {
    const t = turn({ draftingSignaledAt: 1000 });
    const rows = derivePhases(t);
    expect(phase(rows, "explore").status).toBe("done");
    expect(phase(rows, "draft").status).toBe("active");
    expect(phase(rows, "test").status).toBe("pending");
  });

  it("test goes active once a block starts running", () => {
    const t = turn({
      draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
      blocks: [block({ state: "running", endedAt: null })],
    });
    const rows = derivePhases(t);
    expect(phase(rows, "draft").status).toBe("done");
    expect(phase(rows, "test").status).toBe("active");
  });
});

describe("derivePhases — terminal", () => {
  it("terminal success: all done, Done gets a green check", () => {
    const t = turn({
      terminal: "response",
      designEnded: true,
      draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
      blocks: [block()],
    });
    const rows = derivePhases(t);
    expect(rows.map((r) => r.status)).toEqual(["done", "done", "done", "done"]);
  });

  it("a failed block marks Test fail with a failed-count stub", () => {
    const t = turn({
      terminal: "response",
      designEnded: true,
      draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
      blocks: [block({ state: "failed" })],
    });
    const rows = derivePhases(t);
    expect(phase(rows, "test").status).toBe("fail");
    expect(phase(rows, "test").stub).toBe("1 block · failed");
  });

  it("draft-only turn (update_workflow, no run): Test renders notrun, Done still succeeds", () => {
    const t = turn({
      terminal: "response",
      designEnded: true,
      draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
      designActivity: [
        entry({ id: "1", kind: "tool_call", toolName: "update_workflow" }),
      ],
    });
    const rows = derivePhases(t);
    expect(phase(rows, "test").status).toBe("notrun");
    expect(phase(rows, "test").stub).toBe("not run");
    expect(phase(rows, "done").status).toBe("done");
  });

  it("cancel-mid-explore: explore stops, Done renders dim", () => {
    const t = turn({
      terminal: "response",
      cancelled: true,
      designActivity: [
        entry({ id: "1", kind: "tool_call", toolName: "navigate_browser" }),
      ],
    });
    const rows = derivePhases(t);
    expect(phase(rows, "explore").status).toBe("stopped");
    expect(phase(rows, "done").status).toBe("stopped");
  });

  it("error terminal marks the active-at-end phase failed", () => {
    const t = turn({
      terminal: "error",
      designActivity: [
        entry({ id: "1", kind: "tool_call", toolName: "navigate_browser" }),
      ],
    });
    const rows = derivePhases(t);
    expect(phase(rows, "explore").status).toBe("fail");
    expect(phase(rows, "done").status).toBe("fail");
  });
});

describe("showPhaseChecklist", () => {
  it("false for a clarify terminal turn with no draft and no blocks", () => {
    expect(
      showPhaseChecklist(
        turn({ terminal: "response", draft: null, blocks: [] }),
      ),
    ).toBe(false);
  });

  it("true for a hydrated build payload", () => {
    expect(
      showPhaseChecklist(
        turn({
          terminal: "response",
          draft: { blockCount: 1, blockLabels: ["a"], summary: null },
        }),
      ),
    ).toBe(true);
  });

  it("true for any live (non-terminal) turn once design has started", () => {
    expect(showPhaseChecklist(turn({ terminal: null }))).toBe(true);
  });
});

describe("shouldArmDraftingGapTimer", () => {
  it("arms once a tool_call round-trip has completed and gone quiet", () => {
    const t = turn({
      designActivity: [
        entry({ id: "1", kind: "tool_call", toolName: "navigate_browser" }),
        entry({ id: "2", kind: "tool_result", toolName: "navigate_browser" }),
      ],
      lastActivityAtMs: 1000,
    });
    expect(shouldArmDraftingGapTimer(t)).toBe(true);
  });

  it("REGRESSION PIN: does not arm while a tool_call is still pending its result (Codex catch)", () => {
    // A slow navigate_browser taking >8s must not be mistaken for silent
    // LLM code generation — the FE can tell "tool executing" from "LLM
    // generating" by whether the last frame is an unresolved tool_call.
    const t = turn({
      designActivity: [
        entry({ id: "1", kind: "tool_call", toolName: "navigate_browser" }),
      ],
      lastActivityAtMs: 1000,
    });
    expect(shouldArmDraftingGapTimer(t)).toBe(false);
  });

  it("REGRESSION PIN: does not arm when a mid-flight progress narration is the last frame but the tool call is still pending (Codex catch)", () => {
    // schedule_narration can emit a TOOL_STARTED progress narration right
    // after the tool_call, before the matching tool_result — a check on
    // only the last entry's kind misses this; id-matching does not.
    const t = turn({
      designActivity: [
        entry({
          id: "tc-1",
          kind: "tool_call",
          toolName: "navigate_browser",
        }),
        entry({ id: "n-1", kind: "narration", text: "Opening the page…" }),
      ],
      lastActivityAtMs: 1000,
    });
    expect(shouldArmDraftingGapTimer(t)).toBe(false);
  });

  it("arms once the pending tool_call's matching tool_result arrives", () => {
    const t = turn({
      designActivity: [
        entry({
          id: "tc-1",
          kind: "tool_call",
          toolName: "navigate_browser",
        }),
        entry({ id: "n-1", kind: "narration", text: "Opening the page…" }),
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "navigate_browser",
        }),
      ],
      lastActivityAtMs: 1000,
    });
    expect(shouldArmDraftingGapTimer(t)).toBe(true);
  });

  it("does not arm once draftingSignaledAt is already set", () => {
    const t = turn({
      designActivity: [
        entry({ id: "1", kind: "tool_call", toolName: "navigate_browser" }),
        entry({ id: "2", kind: "tool_result", toolName: "navigate_browser" }),
      ],
      lastActivityAtMs: 1000,
      draftingSignaledAt: 500,
    });
    expect(shouldArmDraftingGapTimer(t)).toBe(false);
  });

  it("does not arm once a draft or block exists", () => {
    const t = turn({
      designActivity: [
        entry({ id: "1", kind: "tool_call", toolName: "navigate_browser" }),
        entry({ id: "2", kind: "tool_result", toolName: "navigate_browser" }),
      ],
      lastActivityAtMs: 1000,
      draft: { blockCount: 1, blockLabels: ["a"], summary: null },
    });
    expect(shouldArmDraftingGapTimer(t)).toBe(false);
  });
});

describe("derivePhases — redraft re-activation (SKY-11970 pin)", () => {
  it("THE PIN: a not_demonstrated verdict followed by new activity re-activates Draft, not Test", () => {
    const t = turn({
      terminal: null,
      designEnded: true,
      authoringCount: 1,
      activitySeq: 3,
      draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
      blocks: [block({ state: "completed" })],
      lastRunOutcome: {
        verdict: "not_demonstrated",
        displayReason: "outcome not confirmed",
        activitySeqAtVerdict: 2,
      },
      designActivity: [
        entry({ id: "1", kind: "tool_call", toolName: "navigate_browser" }),
        entry({
          id: "2",
          kind: "tool_call",
          toolName: "update_and_run_blocks",
        }),
        entry({
          id: "3",
          kind: "tool_call",
          toolName: "inspect_current_workflow",
        }),
      ],
    });
    const rows = derivePhases(t);
    expect(phase(rows, "draft").status).toBe("active");
    expect(phase(rows, "test").status).toBe("done");
  });

  it("REGRESSION PIN: the failed run's own guaranteed trailing tool_result does NOT falsely trigger redrafting (Codex catch)", () => {
    // update_and_run_blocks emits run_outcome (the verdict) from inside the
    // tool, then its own tool_result fires right after as the call closes —
    // that tool_result is not new agent work and must not look like one.
    const t = turn({
      terminal: null,
      designEnded: true,
      authoringCount: 1,
      activitySeq: 2, // unchanged: the trailing tool_result never bumped it
      draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
      blocks: [block({ state: "completed" })],
      lastRunOutcome: {
        verdict: "not_demonstrated",
        displayReason: "outcome not confirmed",
        activitySeqAtVerdict: 2,
      },
      designActivity: [
        entry({ id: "1", kind: "tool_call", toolName: "navigate_browser" }),
        entry({
          id: "2",
          kind: "tool_call",
          toolName: "update_and_run_blocks",
        }),
        entry({
          id: "3",
          kind: "tool_result",
          toolName: "update_and_run_blocks",
          success: false,
        }),
      ],
    });
    const rows = derivePhases(t);
    expect(phase(rows, "test").status).toBe("active");
    expect(phase(rows, "draft").status).not.toBe("active");
  });

  it("REGRESSION PIN: a background narration reporting the failed run does NOT falsely trigger redrafting (Codex catch)", () => {
    // schedule_narration in streaming_adapter.py can fire right after the
    // failed run's own tool_result, independent of any real revision.
    const t = turn({
      terminal: null,
      designEnded: true,
      authoringCount: 1,
      activitySeq: 2, // unchanged: narration never bumps it
      draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
      blocks: [block({ state: "completed" })],
      lastRunOutcome: {
        verdict: "not_demonstrated",
        displayReason: "outcome not confirmed",
        activitySeqAtVerdict: 2,
      },
      designActivity: [
        entry({ id: "1", kind: "tool_call", toolName: "navigate_browser" }),
        entry({
          id: "2",
          kind: "tool_call",
          toolName: "update_and_run_blocks",
        }),
        entry({
          id: "3",
          kind: "narration",
          text: "The run didn't confirm the goal was met.",
        }),
      ],
    });
    const rows = derivePhases(t);
    expect(phase(rows, "test").status).toBe("active");
    expect(phase(rows, "draft").status).not.toBe("active");
  });

  it("REGRESSION PIN: redraft detection survives the MAX_DESIGN_ACTIVITY_ENTRIES eviction cap (Codex catch)", () => {
    // designActivity is capped at 50 entries (appendCapped), so its length
    // plateaus at 50 forever once full — a length-based comparison would
    // never detect further activity. activitySeq is uncapped and keeps
    // counting, so redrafting still fires.
    const cappedActivity = Array.from({ length: 50 }, (_, i) =>
      entry({ id: `e${i}`, kind: "narration" }),
    );
    const t = turn({
      terminal: null,
      designEnded: true,
      authoringCount: 1,
      activitySeq: 53,
      draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
      blocks: [block({ state: "completed" })],
      lastRunOutcome: {
        verdict: "not_demonstrated",
        displayReason: "outcome not confirmed",
        activitySeqAtVerdict: 50,
      },
      designActivity: cappedActivity,
    });
    expect(phase(derivePhases(t), "draft").status).toBe("active");
  });

  it("a running block after the redraft flips active back to Test (cyclic accordion)", () => {
    const t = turn({
      terminal: null,
      designEnded: true,
      authoringCount: 2,
      activitySeq: 3,
      draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
      blocks: [block({ state: "running", endedAt: null })],
      lastRunOutcome: {
        verdict: "not_demonstrated",
        displayReason: "outcome not confirmed",
        activitySeqAtVerdict: 2,
      },
      designActivity: [
        entry({ id: "1", kind: "tool_call", toolName: "navigate_browser" }),
        entry({
          id: "2",
          kind: "tool_call",
          toolName: "update_and_run_blocks",
        }),
        entry({ id: "3", kind: "narration" }),
      ],
    });
    expect(phase(derivePhases(t), "test").status).toBe("active");
  });

  it("an evaluating verdict keeps Test active, never Draft", () => {
    const t = turn({
      terminal: null,
      designEnded: true,
      authoringCount: 1,
      activitySeq: 2,
      draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
      blocks: [block({ state: "completed" })],
      lastRunOutcome: {
        verdict: "evaluating",
        displayReason: null,
        activitySeqAtVerdict: 2,
      },
      designActivity: [
        entry({ id: "1", kind: "tool_call", toolName: "navigate_browser" }),
        entry({
          id: "2",
          kind: "tool_call",
          toolName: "update_and_run_blocks",
        }),
      ],
    });
    expect(phase(derivePhases(t), "test").status).toBe("active");
  });

  it("a failed verdict with no new activity since (still composing the terminal reply) stays active on Test, not Draft", () => {
    // activitySeq === activitySeqAtVerdict: no frame has arrived since the
    // verdict, so this can't yet be distinguished from the agent composing
    // its give-up terminal response — redrafting requires proof.
    const t = turn({
      terminal: null,
      designEnded: true,
      authoringCount: 1,
      activitySeq: 2,
      draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
      blocks: [block({ state: "completed" })],
      lastRunOutcome: {
        verdict: "not_demonstrated",
        displayReason: "outcome not confirmed",
        activitySeqAtVerdict: 2,
      },
      designActivity: [
        entry({ id: "1", kind: "tool_call", toolName: "navigate_browser" }),
        entry({
          id: "2",
          kind: "tool_call",
          toolName: "update_and_run_blocks",
        }),
      ],
    });
    const rows = derivePhases(t);
    expect(phase(rows, "test").status).toBe("active");
    expect(phase(rows, "draft").status).not.toBe("active");
  });
});
