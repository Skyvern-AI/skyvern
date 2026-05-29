import { describe, expect, it } from "vitest";

import {
  EMPTY_NARRATIVE,
  TurnNarrativeState,
  applyNarrativeEvent,
  effectiveMode,
} from "./narrativeState";
import {
  WorkflowCopilotBlockProgressUpdate,
  WorkflowCopilotDesignEndUpdate,
  WorkflowCopilotDesignStartUpdate,
  WorkflowCopilotStreamErrorUpdate,
  WorkflowCopilotStreamResponseUpdate,
  WorkflowCopilotTurnStartUpdate,
  WorkflowCopilotWorkflowDraftUpdate,
} from "./workflowCopilotTypes";

const turnStart = (
  overrides: Partial<WorkflowCopilotTurnStartUpdate> = {},
): WorkflowCopilotTurnStartUpdate => ({
  type: "turn_start",
  turn_id: "turn-1",
  turn_index: 0,
  mode: "build",
  timestamp: "2026-05-25T00:00:00Z",
  ...overrides,
});

const designStart = (): WorkflowCopilotDesignStartUpdate => ({
  type: "design_start",
  timestamp: "2026-05-25T00:00:01Z",
});

const designEnd = (): WorkflowCopilotDesignEndUpdate => ({
  type: "design_end",
  timestamp: "2026-05-25T00:00:02Z",
});

const workflowDraft = (
  overrides: Partial<WorkflowCopilotWorkflowDraftUpdate> = {},
): WorkflowCopilotWorkflowDraftUpdate => ({
  type: "workflow_draft",
  block_count: 2,
  block_labels: ["block_one", "block_two"],
  summary: "two block workflow",
  timestamp: "2026-05-25T00:00:03Z",
  ...overrides,
});

const blockProgress = (
  overrides: Partial<WorkflowCopilotBlockProgressUpdate> &
    Pick<WorkflowCopilotBlockProgressUpdate, "block_label" | "status">,
): WorkflowCopilotBlockProgressUpdate => ({
  type: "block_progress",
  workflow_run_block_id: `wrb_${overrides.block_label}`,
  block_type: "task",
  iteration: 0,
  timestamp: "2026-05-25T00:00:04Z",
  ...overrides,
});

const response = (
  overrides: Partial<WorkflowCopilotStreamResponseUpdate> = {},
): WorkflowCopilotStreamResponseUpdate => ({
  type: "response",
  workflow_copilot_chat_id: "chat-1",
  message: "Done.",
  response_time: "2026-05-25T00:00:05Z",
  proposal_disposition: "auto_applicable",
  ...overrides,
});

const errorUpdate = (
  overrides: Partial<WorkflowCopilotStreamErrorUpdate> = {},
): WorkflowCopilotStreamErrorUpdate => ({
  type: "error",
  error: "Something broke.",
  ...overrides,
});

describe("applyNarrativeEvent — turn_start", () => {
  it("seeds turnId/turnIndex/mode from an empty narrative", () => {
    const next = applyNarrativeEvent(EMPTY_NARRATIVE, turnStart());
    expect(next.turnId).toBe("turn-1");
    expect(next.turnIndex).toBe(0);
    expect(next.mode).toBe("build");
    expect(next.designStarted).toBe(false);
    expect(next.designEnded).toBe(false);
    expect(next.blocks).toEqual([]);
    expect(next.draft).toBeNull();
    expect(next.terminal).toBeNull();
  });

  it("falls back to 'unknown' mode when an empty mode is sent", () => {
    const next = applyNarrativeEvent(EMPTY_NARRATIVE, turnStart({ mode: "" }));
    expect(next.mode).toBe("unknown");
  });

  it("resets prior turn state when a new turn_start arrives mid-stream", () => {
    let s: TurnNarrativeState = EMPTY_NARRATIVE;
    s = applyNarrativeEvent(s, turnStart({ turn_id: "t1", turn_index: 0 }));
    s = applyNarrativeEvent(s, designStart());
    s = applyNarrativeEvent(s, workflowDraft());
    s = applyNarrativeEvent(
      s,
      blockProgress({ block_label: "block_one", status: "running" }),
    );
    s = applyNarrativeEvent(
      s,
      turnStart({ turn_id: "t2", turn_index: 1, mode: "edit" }),
    );

    expect(s).toMatchObject({
      turnId: "t2",
      turnIndex: 1,
      mode: "edit",
      blocks: [],
      draft: null,
      designStarted: false,
      designEnded: false,
      terminal: null,
    });
  });

  it("captures prior_block_count on turn_start", () => {
    const s = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      turnStart({ prior_block_count: 3 }),
    );
    expect(s.priorBlockCount).toBe(3);
  });

  it("treats missing prior_block_count as null (cold-start)", () => {
    const s = applyNarrativeEvent(EMPTY_NARRATIVE, turnStart());
    expect(s.priorBlockCount).toBeNull();
  });
});

describe("applyNarrativeEvent — design phase", () => {
  it("sets designStarted on design_start", () => {
    const s = applyNarrativeEvent(EMPTY_NARRATIVE, designStart());
    expect(s.designStarted).toBe(true);
    expect(s.designEnded).toBe(false);
  });

  it("sets designEnded on design_end", () => {
    let s = applyNarrativeEvent(EMPTY_NARRATIVE, designStart());
    s = applyNarrativeEvent(s, designEnd());
    expect(s.designStarted).toBe(true);
    expect(s.designEnded).toBe(true);
  });

  it("captures workflow_draft summary, count, and labels", () => {
    const s = applyNarrativeEvent(EMPTY_NARRATIVE, workflowDraft());
    expect(s.draft).toEqual({
      blockCount: 2,
      blockLabels: ["block_one", "block_two"],
      summary: "two block workflow",
    });
  });

  it("last workflow_draft wins on multi-iteration designs", () => {
    let s = applyNarrativeEvent(EMPTY_NARRATIVE, workflowDraft());
    s = applyNarrativeEvent(
      s,
      workflowDraft({
        block_count: 3,
        block_labels: ["block_one", "block_two", "block_three"],
        summary: "expanded workflow",
      }),
    );
    expect(s.draft).toEqual({
      blockCount: 3,
      blockLabels: ["block_one", "block_two", "block_three"],
      summary: "expanded workflow",
    });
  });
});

describe("applyNarrativeEvent — block_progress", () => {
  it("appends a new block entry on first sighting", () => {
    const s = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      blockProgress({ block_label: "block_one", status: "running" }),
    );
    expect(s.blocks).toEqual([
      {
        workflowRunBlockId: "wrb_block_one",
        label: "block_one",
        blockType: "task",
        state: "running",
        lastSeenIteration: 0,
        activity: [],
        startedAt: "2026-05-25T00:00:04Z",
        endedAt: null,
      },
    ]);
  });

  it("upserts in place when the same workflow_run_block_id is seen again", () => {
    let s = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      blockProgress({ block_label: "block_one", status: "running" }),
    );
    s = applyNarrativeEvent(
      s,
      blockProgress({
        block_label: "block_one",
        status: "completed",
        iteration: 2,
        block_type: "task",
      }),
    );
    expect(s.blocks).toHaveLength(1);
    expect(s.blocks[0]).toMatchObject({
      label: "block_one",
      state: "completed",
      lastSeenIteration: 2,
    });
  });

  it("keeps loop iterations as distinct rows when they share a block_label", () => {
    let s = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      blockProgress({
        block_label: "iterate_url",
        status: "running",
        workflow_run_block_id: "wrb_1",
      }),
    );
    s = applyNarrativeEvent(
      s,
      blockProgress({
        block_label: "iterate_url",
        status: "running",
        workflow_run_block_id: "wrb_2",
      }),
    );
    expect(s.blocks).toHaveLength(2);
    expect(s.blocks.map((b) => b.workflowRunBlockId)).toEqual([
      "wrb_1",
      "wrb_2",
    ]);
  });

  it.each([
    ["failed", "failed"],
    ["terminated", "failed"],
    ["timed_out", "failed"],
    ["canceled", "failed"],
    ["skipped", "skipped"],
    ["queued", "queued"],
    ["something_new", "queued"],
  ])("maps raw status %p to UI state %p", (raw, expected) => {
    const s = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      blockProgress({ block_label: "b", status: raw }),
    );
    expect(s.blocks[0]?.state).toBe(expected);
  });

  it("clears endedAt on retry-back-to-running so stale elapsed disappears", () => {
    let s = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      blockProgress({ block_label: "b", status: "running" }),
    );
    s = applyNarrativeEvent(
      s,
      blockProgress({
        block_label: "b",
        status: "failed",
        timestamp: "2026-05-25T00:01:00Z",
      }),
    );
    expect(s.blocks[0]?.endedAt).toBe("2026-05-25T00:01:00Z");
    s = applyNarrativeEvent(
      s,
      blockProgress({
        block_label: "b",
        status: "running",
        timestamp: "2026-05-25T00:01:30Z",
      }),
    );
    expect(s.blocks[0]?.state).toBe("running");
    expect(s.blocks[0]?.endedAt).toBeNull();
  });

  it("overwrites endedAt with the latest terminal wall clock", () => {
    let s = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      blockProgress({ block_label: "b", status: "running" }),
    );
    s = applyNarrativeEvent(
      s,
      blockProgress({
        block_label: "b",
        status: "failed",
        timestamp: "2026-05-25T00:01:00Z",
      }),
    );
    s = applyNarrativeEvent(
      s,
      blockProgress({
        block_label: "b",
        status: "running",
        timestamp: "2026-05-25T00:01:30Z",
      }),
    );
    s = applyNarrativeEvent(
      s,
      blockProgress({
        block_label: "b",
        status: "completed",
        timestamp: "2026-05-25T00:02:15Z",
      }),
    );
    expect(s.blocks[0]?.state).toBe("completed");
    expect(s.blocks[0]?.endedAt).toBe("2026-05-25T00:02:15Z");
  });
});

describe("applyNarrativeEvent — terminal", () => {
  it("response closes designEnded and uses narrative_summary when present", () => {
    let s = applyNarrativeEvent(EMPTY_NARRATIVE, designStart());
    s = applyNarrativeEvent(
      s,
      response({
        message: "full message text",
        narrative_summary: "one-liner",
      }),
    );
    expect(s.designEnded).toBe(true);
    expect(s.terminal).toBe("response");
    expect(s.terminalMessage).toBe("full message text");
    expect(s.narrativeSummary).toBe("one-liner");
  });

  it("response falls back to message when narrative_summary is null", () => {
    const s = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      response({ message: "full text", narrative_summary: null }),
    );
    expect(s.narrativeSummary).toBe("full text");
  });

  it("response closes design even when design_end was never emitted (CORR-3)", () => {
    let s = applyNarrativeEvent(EMPTY_NARRATIVE, designStart());
    expect(s.designEnded).toBe(false);
    s = applyNarrativeEvent(s, response());
    expect(s.designEnded).toBe(true);
  });

  it("error closes designEnded and leaves narrativeSummary null when absent", () => {
    let s = applyNarrativeEvent(EMPTY_NARRATIVE, designStart());
    s = applyNarrativeEvent(s, errorUpdate({ narrative_summary: null }));
    expect(s.designEnded).toBe(true);
    expect(s.terminal).toBe("error");
    expect(s.terminalMessage).toBe("Something broke.");
    expect(s.narrativeSummary).toBeNull();
  });

  it("error uses narrative_summary when populated", () => {
    const s = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      errorUpdate({ narrative_summary: "refused: too risky" }),
    );
    expect(s.narrativeSummary).toBe("refused: too risky");
  });
});

describe("effectiveMode", () => {
  it("reports build when classifier said unknown but blocks were drafted from empty prior", () => {
    const s: TurnNarrativeState = {
      ...EMPTY_NARRATIVE,
      mode: "unknown",
      draft: { blockCount: 2, blockLabels: ["a", "b"], summary: null },
      terminal: "response",
    };
    expect(effectiveMode(s)).toBe("build");
  });

  it("reports edit when prior_block_count > 0 and turn drafted blocks", () => {
    const s: TurnNarrativeState = {
      ...EMPTY_NARRATIVE,
      mode: "unknown",
      draft: { blockCount: 2, blockLabels: ["a", "b"], summary: null },
      terminal: "response",
      priorBlockCount: 2,
    };
    expect(effectiveMode(s)).toBe("edit");
  });

  it("reports clarify when classifier said draft_only but no blocks were drafted", () => {
    const s: TurnNarrativeState = {
      ...EMPTY_NARRATIVE,
      mode: "draft_only",
      draft: null,
      terminal: "response",
    };
    expect(effectiveMode(s)).toBe("clarify");
  });

  it("preserves docs_answer / diagnose / refuse when terminal has no blocks", () => {
    for (const mode of ["docs_answer", "diagnose", "refuse"]) {
      const s: TurnNarrativeState = {
        ...EMPTY_NARRATIVE,
        mode,
        terminal: "response",
      };
      expect(effectiveMode(s)).toBe(mode);
    }
  });

  it("falls back to classifier mode while turn is still in-flight (no terminal)", () => {
    const s: TurnNarrativeState = {
      ...EMPTY_NARRATIVE,
      mode: "build",
      terminal: null,
    };
    expect(effectiveMode(s)).toBe("build");
  });
});
