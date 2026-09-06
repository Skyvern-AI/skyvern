import { describe, expect, it } from "vitest";

import { derivePhases } from "./copilotPhases";
import {
  ActivityEntry,
  EMPTY_NARRATIVE,
  TurnNarrativeState,
  applyNarrativeEvent,
  condenseActivityEntries,
  humanizeJudgeText,
  hydrateHistoryNarrative,
  hydrateNarrativeFromPayload,
  mapBlockStatus,
  terminalNarrativeText,
} from "./narrativeState";

import {
  WorkflowCopilotBlockProgressUpdate,
  WorkflowCopilotDesignEndUpdate,
  WorkflowCopilotDesignStartUpdate,
  WorkflowCopilotNarrationUpdate,
  WorkflowCopilotStreamErrorUpdate,
  WorkflowCopilotStreamResponseUpdate,
  WorkflowCopilotToolCallUpdate,
  WorkflowCopilotToolResultUpdate,
  WorkflowCopilotTurnStartUpdate,
  WorkflowCopilotWorkflowDraftUpdate,
} from "./workflowCopilotTypes";

describe("terminalNarrativeText — budget expiry", () => {
  it("keeps authored drain text unchanged", () => {
    const turn = {
      ...EMPTY_NARRATIVE,
      terminal: "response" as const,
      narrativeSummary: "Earlier build activity.",
      terminalMessage: "I saved the useful draft.",
      budgetExpiry: {
        budgetExpired: true as const,
        source: "deadline" as const,
        reportProduced: true,
        stagedDraftId: "wf_draft",
        drainFingerprint: "drain-1",
      },
    };
    expect(terminalNarrativeText(turn)).toBe("I saved the useful draft.");
  });

  it.each([
    [
      "deadline",
      null,
      "This turn reached its time limit without producing a report.",
    ],
    [
      "max_turns",
      null,
      "This turn reached its model-call limit without producing a report.",
    ],
    [
      "max_turns",
      "wf_draft",
      "This turn reached its model-call limit without producing a report. A draft was staged during this session.",
    ],
  ] as const)(
    "renders honest no-report status for %s with draft %s",
    (source, stagedDraftId, expected) => {
      const turn: TurnNarrativeState = {
        ...EMPTY_NARRATIVE,
        terminal: "error",
        proposalDisposition: "no_proposal",
        narrativeSummary: "Earlier build activity.",
        budgetExpiry: {
          budgetExpired: true,
          source,
          reportProduced: false,
          stagedDraftId,
          drainFingerprint: "drain-2",
        },
      };
      expect(terminalNarrativeText(turn)).toBe(expected);
    },
  );
});

const turnStart = (
  overrides: Partial<WorkflowCopilotTurnStartUpdate> = {},
): WorkflowCopilotTurnStartUpdate => ({
  type: "turn_start",
  turn_id: "turn-1",
  turn_index: 0,
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

const toolResult = (
  overrides: Partial<WorkflowCopilotToolResultUpdate> = {},
): WorkflowCopilotToolResultUpdate => ({
  type: "tool_result",
  tool_name: "update_and_run_blocks",
  success: true,
  summary: "Testing workflow successful",
  iteration: 0,
  tool_call_id: "call-1",
  ...overrides,
});

const narration = (
  overrides: Partial<WorkflowCopilotNarrationUpdate> = {},
): WorkflowCopilotNarrationUpdate => ({
  type: "narration",
  narration: "Reading the page…",
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
  it("seeds turnId/turnIndex from an empty narrative", () => {
    const next = applyNarrativeEvent(EMPTY_NARRATIVE, turnStart());
    expect(next.turnId).toBe("turn-1");
    expect(next.turnIndex).toBe(0);
    expect(next.designStarted).toBe(false);
    expect(next.designEnded).toBe(false);
    expect(next.blocks).toEqual([]);
    expect(next.draft).toBeNull();
    expect(next.terminal).toBeNull();
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
    s = applyNarrativeEvent(s, turnStart({ turn_id: "t2", turn_index: 1 }));

    expect(s).toMatchObject({
      turnId: "t2",
      turnIndex: 1,
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

  it("shows a write's patch at write time, before its run reports back", () => {
    // update_and_run_blocks writes and runs inside one tool call, so waiting for the
    // tool_result would put the patch on screen only after the test had finished.
    const diffs = [
      { label: "star_count", added: 15, removed: 0, patch: "@@\n+code" },
    ];
    let s = applyNarrativeEvent(EMPTY_NARRATIVE, toolCall());
    s = applyNarrativeEvent(
      s,
      workflowDraft({ code_diffs: diffs, tool_call_id: "call-1" }),
    );

    // No tool_result yet: the run is still in flight.
    const call = s.designActivity.find((e) => e.id === "tc-call-1");
    expect(call?.codeDiffs).toHaveLength(1);
    expect(call?.codeDiffs?.[0]).toMatchObject({
      label: "star_count",
      added: 15,
      removed: 0,
      patch: "@@\n+code",
    });
    expect(s.designActivity.some((e) => e.id === "tr-call-1")).toBe(false);
  });

  it("shows a repair patch while its active block is still running", () => {
    const diffs = [
      { label: "star_count", added: 4, removed: 2, patch: "@@\n-code\n+fixed" },
    ];
    let s = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      blockProgress({ block_label: "star_count", status: "running" }),
    );
    s = applyNarrativeEvent(s, toolCall());

    expect(s.blocks[0]?.activity[0]?.id).toBe("tc-call-1");
    s = applyNarrativeEvent(
      s,
      workflowDraft({
        block_count: 1,
        block_labels: ["star_count"],
        code_diffs: diffs,
        tool_call_id: "call-1",
      }),
    );

    // No tool_result yet: a repair nested under the active block must expose
    // the write at the same early seam as an initial design-phase write.
    expect(s.blocks[0]?.activity[0]?.codeDiffs).toMatchObject([
      { label: "star_count", added: 4, removed: 2, patch: "@@\n-code\n+fixed" },
    ]);
    expect(s.designActivity.some((e) => e.id === "tr-call-1")).toBe(false);
  });

  it("a draft naming no call leaves the activity log untouched", () => {
    // Older backends send no tool_call_id; the row must not guess which call to attach to.
    let s = applyNarrativeEvent(EMPTY_NARRATIVE, toolCall());
    const before = s.designActivity;
    s = applyNarrativeEvent(s, workflowDraft());

    expect(s.designActivity).toEqual(before);
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
    ["canceled", "stopped"],
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

  it("co-locates a run tool's result with its call after a block starts running (SKY-12831)", () => {
    // The call lands in designActivity (no block running yet); the run then
    // flips a block to running. The result must rejoin the call in
    // designActivity so the pair folds — not land in the block card.
    const afterCall = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      toolCall({ tool_call_id: "call-1" }),
    );
    const afterBlock = applyNarrativeEvent(
      afterCall,
      blockProgress({ block_label: "step_1", status: "running" }),
    );
    const s = applyNarrativeEvent(
      afterBlock,
      toolResult({ tool_call_id: "call-1" }),
    );

    expect(s.designActivity.map((e) => e.id)).toEqual([
      "tc-call-1",
      "tr-call-1",
    ]);
    expect(
      s.blocks.find((b) => b.label === "step_1")?.activity ?? [],
    ).toHaveLength(0);
    expect(condenseActivityEntries(s.designActivity)).toHaveLength(1);
  });
});

describe("condenseActivityEntries", () => {
  function reduceEvents(events: Parameters<typeof applyNarrativeEvent>[1][]) {
    return events.reduce(
      (state: TurnNarrativeState, event) => applyNarrativeEvent(state, event),
      EMPTY_NARRATIVE,
    );
  }

  it("folds a resolved tool_call/tool_result pair into one row (no leftover calling… chatter)", () => {
    const s = reduceEvents([
      toolCall({ tool_call_id: "call-1", tool_name: "evaluate" }),
      toolResult({
        tool_call_id: "call-1",
        tool_name: "evaluate",
        success: true,
      }),
    ]);
    const condensed = condenseActivityEntries(s.designActivity);
    expect(condensed).toHaveLength(1);
    expect(condensed[0]).toMatchObject({ kind: "tool_result", success: true });
  });

  it("keeps the call timestamp when replacing it with its settled result", () => {
    const condensed = condenseActivityEntries([
      {
        id: "tc-call-1",
        kind: "tool_call",
        text: "Opening page",
        iteration: 0,
        toolName: "navigate_browser",
        timestamp: "2026-05-25T00:00:04Z",
      },
      {
        id: "tr-call-1",
        kind: "tool_result",
        text: "The page did not open",
        iteration: 0,
        toolName: "navigate_browser",
        success: false,
        timestamp: "2026-05-25T00:00:24Z",
      },
    ]);

    expect(condensed).toHaveLength(1);
    expect(condensed[0]).toMatchObject({
      kind: "tool_result",
      activityStartedAt: "2026-05-25T00:00:04Z",
      timestamp: "2026-05-25T00:00:24Z",
    });
  });

  it("REGRESSION PIN: substitutes the humanized tool name for the backend's bare 'OK' fallback (celal QA catch)", () => {
    // A tool with no dedicated backend summary falls back to a literal
    // "OK" — condensing already removed the "<tool> · calling…" row that
    // used to give it context, so a bare "OK" reads as meaningless.
    const s = reduceEvents([
      toolCall({ tool_call_id: "call-1", tool_name: "evaluate" }),
      toolResult({
        tool_call_id: "call-1",
        tool_name: "evaluate",
        summary: "OK",
      }),
    ]);
    const condensed = condenseActivityEntries(s.designActivity);
    expect(condensed).toHaveLength(1);
    expect(condensed[0]?.text).toBe("Inspecting page");
    expect(condensed[0]?.text).not.toBe("OK");
  });

  it("leaves a real (non-fallback) backend summary untouched, even if a mapped tool", () => {
    const s = reduceEvents([
      toolCall({ tool_call_id: "call-1", tool_name: "evaluate" }),
      toolResult({
        tool_call_id: "call-1",
        tool_name: "evaluate",
        summary: "Evaluated JavaScript — returned a string",
      }),
    ]);
    const condensed = condenseActivityEntries(s.designActivity);
    expect(condensed[0]?.text).toBe("Evaluated JavaScript — returned a string");
  });

  it("folds a failed attempt then a retry into one row, terminal outcome only", () => {
    const s = reduceEvents([
      toolCall({ tool_call_id: "call-1", tool_name: "extract" }),
      toolResult({
        tool_call_id: "call-1",
        tool_name: "extract",
        success: false,
        summary: "no results",
      }),
      toolCall({ tool_call_id: "call-2", tool_name: "extract" }),
      toolResult({
        tool_call_id: "call-2",
        tool_name: "extract",
        success: true,
        summary: "top 5 titles",
      }),
    ]);
    const condensed = condenseActivityEntries(s.designActivity);
    expect(condensed).toHaveLength(1);
    expect(condensed[0]).toMatchObject({
      success: true,
      attempts: 2,
      text: "top 5 titles",
    });
  });

  it("folds a 3-attempt retry chain into one row with attempts=3", () => {
    const s = reduceEvents([
      toolCall({ tool_call_id: "c1", tool_name: "extract" }),
      toolResult({ tool_call_id: "c1", tool_name: "extract", success: false }),
      toolCall({ tool_call_id: "c2", tool_name: "extract" }),
      toolResult({ tool_call_id: "c2", tool_name: "extract", success: false }),
      toolCall({ tool_call_id: "c3", tool_name: "extract" }),
      toolResult({ tool_call_id: "c3", tool_name: "extract", success: true }),
    ]);
    const condensed = condenseActivityEntries(s.designActivity);
    expect(condensed).toHaveLength(1);
    expect(condensed[0]).toMatchObject({ success: true, attempts: 3 });
  });

  it("keeps the first attempt's start across a folded retry chain", () => {
    const condensed = condenseActivityEntries([
      {
        id: "tc-c1",
        kind: "tool_call",
        text: "Opening page",
        iteration: 0,
        toolName: "navigate_browser",
        timestamp: "2026-05-25T00:00:04Z",
      },
      {
        id: "tr-c1",
        kind: "tool_result",
        text: "The page did not open",
        iteration: 0,
        toolName: "navigate_browser",
        success: false,
        timestamp: "2026-05-25T00:00:24Z",
      },
      {
        id: "tc-c2",
        kind: "tool_call",
        text: "Opening page",
        iteration: 1,
        toolName: "navigate_browser",
        timestamp: "2026-05-25T00:00:29Z",
      },
      {
        id: "tr-c2",
        kind: "tool_result",
        text: "Opened",
        iteration: 1,
        toolName: "navigate_browser",
        success: true,
        timestamp: "2026-05-25T00:00:49Z",
      },
    ]);

    expect(condensed).toHaveLength(1);
    expect(condensed[0]).toMatchObject({
      attempts: 2,
      activityStartedAt: "2026-05-25T00:00:04Z",
      timestamp: "2026-05-25T00:00:49Z",
    });
  });

  it("keeps overlapping same-tool siblings separate instead of calling them retries", () => {
    const condensed = condenseActivityEntries([
      {
        id: "tr-a",
        kind: "tool_result",
        text: "The first call failed",
        iteration: 0,
        toolName: "navigate_browser",
        success: false,
        activityStartedAt: "2026-05-25T00:00:10Z",
        timestamp: "2026-05-25T00:00:30Z",
      },
      {
        id: "tr-b",
        kind: "tool_result",
        text: "The parallel call succeeded",
        iteration: 1,
        toolName: "navigate_browser",
        success: true,
        activityStartedAt: "2026-05-25T00:00:20Z",
        timestamp: "2026-05-25T00:00:40Z",
      },
    ]);

    expect(condensed).toHaveLength(2);
    expect(condensed.map((entry) => entry.id)).toEqual(["tr-a", "tr-b"]);
    expect(condensed.every((entry) => entry.attempts === undefined)).toBe(true);
  });

  it("folds a retry across a narration sitting between two attempts (narration never breaks the fold)", () => {
    // Array position can't reliably tell "narration between attempts" from
    // "narration mid-flight during the retry itself" (see the regression
    // pin below, where the same ordered shape arises from a genuinely
    // different case) — so narration is never treated as a fold-breaking
    // gap, full stop.
    const s = reduceEvents([
      toolCall({ tool_call_id: "c1", tool_name: "extract" }),
      toolResult({ tool_call_id: "c1", tool_name: "extract", success: false }),
      narration(),
      toolCall({ tool_call_id: "c2", tool_name: "extract" }),
      toolResult({ tool_call_id: "c2", tool_name: "extract", success: true }),
    ]);
    const condensed = condenseActivityEntries(s.designActivity);
    // Narration arrived before the merged (2nd-attempt) result, so the
    // fold keeps that order — narration first, not stranded after a row
    // whose content is chronologically later than it.
    expect(condensed.map((e) => e.kind)).toEqual(["narration", "tool_result"]);
    expect(condensed[1]).toMatchObject({ success: true, attempts: 2 });
  });

  it("REGRESSION PIN: folds a retry when the narration fires mid-flight during the RETRY itself (Codex catch)", () => {
    // Ordering fix (pass 1) puts this narration in the identical array
    // position as narration genuinely between two attempts — attempt 1's
    // clean result already sits before attempt 2's call, so the retry's
    // own mid-flight narration lands right after it either way. Only a
    // narration-agnostic fold (last TOOL row, not literal adjacency)
    // survives this case.
    const s = reduceEvents([
      toolCall({ tool_call_id: "c1", tool_name: "extract" }),
      toolResult({ tool_call_id: "c1", tool_name: "extract", success: false }),
      toolCall({ tool_call_id: "c2", tool_name: "extract" }),
      narration(),
      toolResult({ tool_call_id: "c2", tool_name: "extract", success: true }),
    ]);
    const condensed = condenseActivityEntries(s.designActivity);
    // The merge must not leave the (chronologically later) result parked
    // at attempt 1's old position, ahead of narration that streamed
    // before it arrived.
    expect(condensed.map((e) => e.kind)).toEqual(["narration", "tool_result"]);
    expect(condensed[1]).toMatchObject({ success: true, attempts: 2 });
  });

  it("REGRESSION PIN: folds a retry even when the narration fires mid-flight, during the FIRST attempt (reviewer catch)", () => {
    // The narrator can emit a progress narration while a call is still
    // pending. Pairing (pass 1) then places attempt 1's result at attempt
    // 1's call position, ahead of that narration — which used to make the
    // narration look like it sat BETWEEN the two attempts and break the
    // fold, even though nothing genuinely interrupted the retry.
    const s = reduceEvents([
      toolCall({ tool_call_id: "c1", tool_name: "extract" }),
      narration(),
      toolResult({ tool_call_id: "c1", tool_name: "extract", success: false }),
      toolCall({ tool_call_id: "c2", tool_name: "extract" }),
      toolResult({ tool_call_id: "c2", tool_name: "extract", success: true }),
    ]);
    const condensed = condenseActivityEntries(s.designActivity);
    expect(condensed.map((e) => e.kind)).toEqual(["narration", "tool_result"]);
    expect(condensed[1]).toMatchObject({ success: true, attempts: 2 });
  });

  it("does not fold two consecutive different-tool results", () => {
    const s = reduceEvents([
      toolCall({ tool_call_id: "c1", tool_name: "navigate_browser" }),
      toolResult({
        tool_call_id: "c1",
        tool_name: "navigate_browser",
        success: false,
      }),
      toolCall({ tool_call_id: "c2", tool_name: "evaluate" }),
      toolResult({ tool_call_id: "c2", tool_name: "evaluate", success: true }),
    ]);
    const condensed = condenseActivityEntries(s.designActivity);
    expect(condensed).toHaveLength(2);
  });

  it("keeps an orphaned tool_result (its call was evicted) as its own row instead of dropping it", () => {
    const entries: ActivityEntry[] = [
      {
        kind: "tool_result",
        text: "done",
        iteration: 0,
        toolName: "evaluate",
        success: true,
        id: "tr-orphan-1",
      },
    ];
    expect(condenseActivityEntries(entries)).toEqual(entries);
  });

  it("REGRESSION PIN: still pairs a call/result whose sliced tool_call_id is an empty string (Claude catch)", () => {
    // toolCallIdOf returns "" (falsy but defined) for an id like "tc-" —
    // a truthiness check would treat that as "no id" and never pair it,
    // inconsistent with hasPendingToolCall's unconditional `?? ""`.
    const entries: ActivityEntry[] = [
      {
        kind: "tool_call",
        text: "Extracting…",
        iteration: 0,
        toolName: "extract",
        id: "tc-",
      },
      {
        kind: "tool_result",
        text: "done",
        iteration: 0,
        toolName: "extract",
        success: true,
        id: "tr-",
      },
    ];
    expect(condenseActivityEntries(entries)).toHaveLength(1);
  });

  it("an orphaned same-tool result immediately after a failed attempt still folds as its retry outcome", () => {
    const s = reduceEvents([
      toolCall({ tool_call_id: "c1", tool_name: "extract" }),
      toolResult({ tool_call_id: "c1", tool_name: "extract", success: false }),
    ]);
    const orphan: ActivityEntry = {
      kind: "tool_result",
      text: "top 5 titles + links",
      iteration: 1,
      toolName: "extract",
      success: true,
      id: "tr-evicted-call-2",
    };
    const condensed = condenseActivityEntries([...s.designActivity, orphan]);
    expect(condensed).toHaveLength(1);
    expect(condensed[0]).toMatchObject({
      success: true,
      attempts: 2,
      text: "top 5 titles + links",
    });
  });

  it("leaves a still-pending retry visible, tagged with the attempt count", () => {
    const s = reduceEvents([
      toolCall({
        tool_call_id: "c1",
        tool_name: "extract",
        display_label: "Extracting",
      }),
      toolResult({
        tool_call_id: "c1",
        tool_name: "extract",
        success: false,
        display_label: "Extracting",
      }),
      toolCall({
        tool_call_id: "c2",
        tool_name: "extract",
        display_label: "Extracting",
      }),
    ]);
    const condensed = condenseActivityEntries(s.designActivity);
    expect(condensed).toHaveLength(1);
    expect(condensed[0]).toMatchObject({ kind: "tool_call", attempts: 2 });
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

  it("captures proposal disposition from response events", () => {
    const s = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      response({ proposal_disposition: "review_untested" }),
    );
    expect(s.proposalDisposition).toBe("review_untested");
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

const reproBlock = (label: string): Record<string, unknown> => ({
  workflowRunBlockId: "",
  label,
  blockType: "code",
  state: "completed",
  lastSeenIteration: 0,
  activity: [],
  startedAt: "2026-06-10T07:27:57.458136+00:00",
  endedAt: "2026-06-10T07:28:37.384095+00:00",
});

// Sanitized copy of a persisted false-green repro payload: a build turn that
// drafted and ran 3 blocks but terminated via the loop-guard clarify ask.
const reproClarifyPayload = (
  overrides: Record<string, unknown> = {},
): Record<string, unknown> => ({
  turnId: "turn-repro",
  turnIndex: 0,
  responseType: "REPLY",
  cancelled: false,
  proposalDisposition: "no_proposal",
  designStarted: true,
  designEnded: true,
  draft: {
    blockCount: 3,
    blockLabels: [
      "open_registry_find_registrant",
      "search_jane_doe_credential_a",
      "expand_and_extract_certifications",
    ],
    summary: null,
  },
  blocks: [
    reproBlock("open_registry_find_registrant"),
    reproBlock("search_jane_doe_credential_a"),
    reproBlock("expand_and_extract_certifications"),
  ],
  terminal: "response",
  terminalMessage:
    "I'm stuck retrying the same step. Tell me what to change and I'll try a different approach.",
  narrativeSummary:
    "I'm stuck retrying the same step. Tell me what to change and I'll try a different approach.",
  priorBlockCount: 0,
  designActivity: [],
  startedAt: "2026-06-10T07:22:55.699474+00:00",
  endedAt: "2026-06-10T07:37:57.457019+00:00",
  ...overrides,
});

describe("hydrateNarrativeFromPayload — terminal adjudication fields", () => {
  it("hydrates responseKind without inventing an authoring success stamp", () => {
    const turn = hydrateNarrativeFromPayload(
      reproClarifyPayload({ responseKind: "build" }),
    );
    expect(turn?.responseKind).toBe("build");
  });

  it("hydrates the answer response kind from persisted payloads", () => {
    const turn = hydrateNarrativeFromPayload(
      reproClarifyPayload({ responseKind: "answer" }),
    );
    expect(turn?.responseKind).toBe("answer");
  });

  it("treats absent fields as null", () => {
    const turn = hydrateNarrativeFromPayload(reproClarifyPayload());
    expect(turn?.responseKind).toBeNull();
  });

  it("treats an unknown responseKind as absent", () => {
    const turn = hydrateNarrativeFromPayload(
      reproClarifyPayload({
        responseKind: "celebrate",
      }),
    );
    expect(turn?.responseKind).toBeNull();
  });
});

describe("hydrateHistoryNarrative — persisted turn_outcome graft", () => {
  it("grafts clarify from the adjacent turn_outcome onto a pre-fix payload", () => {
    const turn = hydrateHistoryNarrative(reproClarifyPayload(), {
      response_kind: "clarify",
    })!;
    expect(turn.responseKind).toBe("clarify");
  });

  it("keeps the payload's own responseKind over the graft", () => {
    const turn = hydrateHistoryNarrative(
      reproClarifyPayload({ responseKind: "refuse" }),
      { response_kind: "clarify" },
    )!;
    expect(turn.responseKind).toBe("refuse");
  });

  it("grafts build from the adjacent turn_outcome onto a pre-fix payload", () => {
    const turn = hydrateHistoryNarrative(reproClarifyPayload(), {
      response_kind: "build",
    })!;
    expect(turn.responseKind).toBe("build");
  });

  it("tolerates missing or unknown turn_outcome", () => {
    expect(
      hydrateHistoryNarrative(reproClarifyPayload(), null)?.responseKind,
    ).toBeNull();
    expect(
      hydrateHistoryNarrative(reproClarifyPayload(), {
        response_kind: "celebrate",
      })?.responseKind,
    ).toBeNull();
    expect(
      hydrateHistoryNarrative(null, { response_kind: "clarify" }),
    ).toBeUndefined();
  });
});

describe("applyNarrativeEvent — terminal adjudication on live frames", () => {
  it("carries the adjudication through the response reducer", () => {
    const s = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      response({
        message: "I'm stuck retrying the same step.",
        narrative_payload: reproClarifyPayload({
          responseKind: "clarify",
        }),
      }),
    );
    expect(s.responseKind).toBe("clarify");
  });

  it("leaves both fields null on frames from an older backend", () => {
    const s = applyNarrativeEvent(EMPTY_NARRATIVE, response());
    expect(s.responseKind).toBeNull();
  });
});

describe("humanizeJudgeText", () => {
  // Legacy backend display text still needs to render safely while old events remain in history.
  const JUDGE_REASON =
    "The run completed but did not demonstrate the goal outcome(s). " +
    "Missing evidence: the number of Customer Agents is known. " +
    "Add or fix the block that produces the missing outcome evidence, then re-run.";

  // What actually reaches the client: run_outcome_display_reason truncates display_reason to
  // _DISPLAY_REASON_MAX_CHARS (160), so the trailing instruction always arrives cut mid-word.
  const TRUNCATED_JUDGE_REASON = JUDGE_REASON.slice(0, 160);

  it("rewrites the truncated verdict the backend actually sends", () => {
    expect(TRUNCATED_JUDGE_REASON).toHaveLength(160);
    expect(TRUNCATED_JUDGE_REASON.endsWith("produces the ")).toBe(true);

    expect(humanizeJudgeText(TRUNCATED_JUDGE_REASON)).toBe(
      "The run finished but didn't produce what you asked for: the number of Customer Agents is known.",
    );
  });

  it("still rewrites the untruncated verdict", () => {
    expect(humanizeJudgeText(JUDGE_REASON)).toBe(
      "The run finished but didn't produce what you asked for: the number of Customer Agents is known.",
    );
  });

  it("strips the instruction wherever the 160-char cut lands inside it", () => {
    const instruction =
      " Add or fix the block that produces the missing outcome evidence, then re-run.";
    const head =
      "The run completed but did not demonstrate the goal outcome(s): title check.";
    for (let cut = 0; cut <= instruction.length; cut += 1) {
      const humanized = humanizeJudgeText(head + instruction.slice(0, cut));
      expect(humanized).not.toContain("Add or fix");
      expect(humanized.startsWith("The run finished but didn't produce")).toBe(
        true,
      );
    }
  });

  it("rewrites the verdict where the backend appends it to the closing message", () => {
    const closing = `I ran the workflow, but I could not confirm the goal was met. Reason: ${TRUNCATED_JUDGE_REASON}`;
    const humanized = humanizeJudgeText(closing);

    expect(humanized).toContain("I ran the workflow, but I could not confirm");
    expect(humanized).not.toContain("did not demonstrate the goal outcome");
    expect(humanized).not.toContain("Add or fix");
  });

  it("leaves ordinary assistant prose alone, even when it ends like the instruction", () => {
    // The trailing-prefix scan must never reach free prose: the same helper
    // runs over assistant prose on the headline paths, which is not judge text.
    for (const prose of [
      "Choose option A",
      "Click the button labelled Add",
      "Add or fix the selector yourself",
    ]) {
      expect(humanizeJudgeText(prose)).toBe(prose);
    }
  });

  it("strips the instruction when the backend appends Evidence after it", () => {
    // terminal_envelope.py appends " Evidence: <blocker_reason>" after the Reason sentence.
    const withEvidence =
      "I could not confirm the goal was met. Reason: " +
      JUDGE_REASON +
      " Evidence: the login form never submitted";
    const humanized = humanizeJudgeText(withEvidence);

    expect(humanized).not.toContain("Add or fix");
    expect(humanized).not.toContain("did not demonstrate the goal outcome");
    expect(humanized).toContain("Evidence: the login form never submitted");
  });

  it("passes unrecognized text through untouched", () => {
    const other = "The run stopped because the browser session ended.";
    expect(humanizeJudgeText(other)).toBe(other);
  });

  it("is idempotent, so a re-humanized string is unchanged", () => {
    const once = humanizeJudgeText(TRUNCATED_JUDGE_REASON);
    expect(humanizeJudgeText(once)).toBe(once);
  });
});

describe("a user stop renders as stopped, never as a failure", () => {
  it("maps a canceled block to stopped, keeping the other halt states failed", () => {
    expect(mapBlockStatus("canceled")).toBe("stopped");
    expect(mapBlockStatus("failed")).toBe("failed");
    expect(mapBlockStatus("terminated")).toBe("failed");
    expect(mapBlockStatus("timed_out")).toBe("failed");
  });

  it("records a canceled block as a terminal state so its elapsed pill freezes", () => {
    let s = applyNarrativeEvent(EMPTY_NARRATIVE, turnStart());
    s = applyNarrativeEvent(
      s,
      blockProgress({ block_label: "login", status: "running" }),
    );
    s = applyNarrativeEvent(
      s,
      blockProgress({ block_label: "login", status: "canceled" }),
    );

    const block = s.blocks.find((b) => b.label === "login");
    expect(block?.state).toBe("stopped");
    expect(block?.endedAt).not.toBeNull();
  });

  it("keeps a stopped block through hydration so a reload does not downgrade it", () => {
    const turn = hydrateNarrativeFromPayload({
      turnId: "turn-1",
      turnIndex: 0,
      designStarted: true,
      designEnded: true,
      draft: null,
      blocks: [
        {
          workflowRunBlockId: "wrb_1",
          label: "login",
          blockType: "task",
          state: "stopped",
          lastSeenIteration: 0,
          activity: [],
          startedAt: "2026-05-25T00:00:01Z",
          endedAt: "2026-05-25T00:00:04Z",
        },
      ],
      terminal: "response",
      terminalMessage: "Stopped.",
      narrativeSummary: null,
      priorBlockCount: null,
      designActivity: [],
      startedAt: "2026-05-25T00:00:00Z",
      endedAt: "2026-05-25T00:00:05Z",
    });

    expect(turn?.blocks[0]?.state).toBe("stopped");
  });
});

// The backend stamps a canceled block "failed" and a canceled turn's terminal
// "error" (_BLOCK_STATUS_TO_UI_STATE, agent.py), so these payloads are the
// shape a real cancel actually delivers — not the "stopped" shape the live
// block_progress frames produce.
describe("a real cancel's backend payload still renders neutrally", () => {
  const cancelledPayload = () => ({
    turnId: "turn-1",
    turnIndex: 0,
    designStarted: true,
    designEnded: true,
    draft: null,
    blocks: [
      {
        workflowRunBlockId: "wrb_1",
        label: "log_in",
        blockType: "task",
        state: "failed",
        lastSeenIteration: 0,
        activity: [],
        startedAt: "2026-05-25T00:00:01Z",
        endedAt: "2026-05-25T00:00:04Z",
      },
    ],
    terminal: "error",
    terminalMessage:
      "Stopped. 1 block ran this turn. No workflow draft from this turn was preserved.",
    narrativeSummary: null,
    cancelled: true,
    priorBlockCount: null,
    designActivity: [],
    startedAt: "2026-05-25T00:00:00Z",
    endedAt: "2026-05-25T00:00:05Z",
  });

  it("re-reads a backend-failed block on a cancelled turn as stopped", () => {
    const turn = hydrateNarrativeFromPayload(cancelledPayload());
    expect(turn?.blocks[0]?.state).toBe("stopped");
  });

  it("does not redden the rail for a run the user stopped", () => {
    const turn = hydrateNarrativeFromPayload(cancelledPayload())!;
    const phases = derivePhases(turn);
    const byId = Object.fromEntries(phases.map((p) => [p.id, p.status]));
    expect(byId.done).toBe("stopped");
    expect(byId.test).toBe("stopped");
  });

  it("keeps a genuine error turn's block failed when it was not cancelled", () => {
    const turn = hydrateNarrativeFromPayload({
      ...cancelledPayload(),
      cancelled: false,
    })!;
    expect(turn.blocks[0]?.state).toBe("failed");
  });
});
