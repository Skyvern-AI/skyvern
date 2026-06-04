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
  WorkflowCopilotToolCallUpdate,
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

const toolCall = (
  overrides: Partial<WorkflowCopilotToolCallUpdate> = {},
): WorkflowCopilotToolCallUpdate => ({
  type: "tool_call",
  tool_name: "update_and_run_blocks",
  display_label: "Testing workflow",
  tool_input: {},
  iteration: 0,
  tool_call_id: "call-1",
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

describe("applyNarrativeEvent — activity", () => {
  it("renders product-safe labels for internal tool calls", () => {
    const s = applyNarrativeEvent(EMPTY_NARRATIVE, toolCall());

    expect(s.designActivity).toHaveLength(1);
    expect(s.designActivity[0]).toMatchObject({
      kind: "tool_call",
      toolName: "update_and_run_blocks",
      displayLabel: "Testing workflow",
      text: "Testing workflow…",
    });
    expect(s.designActivity[0]?.text).not.toContain("update_and_run_blocks");
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

  it("response uses backend error narrative payload when present", () => {
    const s = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      response({
        message:
          "Copilot hit an internal error before it could finish this turn.",
        narrative_payload: {
          turnId: "turn-1",
          turnIndex: 0,
          mode: "build",
          designStarted: true,
          designEnded: true,
          draft: null,
          blocks: [
            {
              workflowRunBlockId: "wrb_1",
              label: "draft_workflow",
              blockType: "task",
              state: "running",
              lastSeenIteration: 0,
              activity: [],
              startedAt: "2026-05-25T00:00:01Z",
              endedAt: null,
            },
          ],
          terminal: "error",
          terminalMessage:
            "Copilot hit an internal error before it could finish this turn.",
          narrativeSummary: "Copilot hit an internal error.",
          priorBlockCount: null,
          designActivity: [],
          startedAt: "2026-05-25T00:00:00Z",
          endedAt: "2026-05-25T00:00:05Z",
        },
      }),
    );

    expect(s.terminal).toBe("error");
    expect(s.narrativeSummary).toBe("Copilot hit an internal error.");
    expect(s.blocks).toHaveLength(1);
    expect(s.blocks[0]?.state).toBe("failed");
  });

  it("response preserves ASK_QUESTION classification for summary mode", () => {
    const s = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      response({
        message: "Please provide the exact registry URL.",
        response_type: "ASK_QUESTION",
        narrative_payload: {
          turnId: "turn-1",
          turnIndex: 0,
          mode: "diagnose",
          designStarted: true,
          designEnded: true,
          draft: null,
          blocks: [],
          terminal: "response",
          terminalMessage: "Please provide the exact registry URL.",
          narrativeSummary: "Please provide the exact registry URL.",
          priorBlockCount: null,
          designActivity: [],
          startedAt: "2026-05-25T00:00:00Z",
          endedAt: "2026-05-25T00:00:05Z",
        },
      }),
    );

    expect(s.responseType).toBe("ASK_QUESTION");
    expect(effectiveMode(s)).toBe("clarify");
  });

  it("preserves cancelled responses with drafts as response terminals", () => {
    let s = applyNarrativeEvent(EMPTY_NARRATIVE, turnStart());
    s = applyNarrativeEvent(
      s,
      workflowDraft({
        block_count: 2,
        block_labels: ["open_page", "add_to_cart"],
      }),
    );
    s = applyNarrativeEvent(
      s,
      response({
        cancelled: true,
        message:
          "Cancelled. I have a draft workflow you can keep -- accept it to save, or discard.",
        proposal_disposition: "review_untested",
      }),
    );

    expect(s.terminal).toBe("response");
    expect(s.draft?.blockCount).toBe(2);
    expect(s.blocks.map((b) => b.state)).toEqual(["drafted", "drafted"]);
    expect(effectiveMode(s)).toBe("build");
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

  it("reports clarify when backend classified the response as ASK_QUESTION", () => {
    const s: TurnNarrativeState = {
      ...EMPTY_NARRATIVE,
      mode: "diagnose",
      responseType: "ASK_QUESTION",
      terminal: "response",
    };
    expect(effectiveMode(s)).toBe("clarify");
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
