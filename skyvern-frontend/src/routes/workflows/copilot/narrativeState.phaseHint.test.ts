import { describe, expect, it } from "vitest";

import {
  CopilotPhaseHintEvent,
  EMPTY_NARRATIVE,
  TurnNarrativeState,
  applyNarrativeEvent,
  hydrateNarrativeFromPayload,
} from "./narrativeState";
import {
  WorkflowCopilotBlockProgressUpdate,
  WorkflowCopilotRunOutcomeUpdate,
  WorkflowCopilotStreamResponseUpdate,
  WorkflowCopilotToolCallUpdate,
  WorkflowCopilotToolResultUpdate,
  WorkflowCopilotTurnStartUpdate,
} from "./workflowCopilotTypes";

const turnStart = (): WorkflowCopilotTurnStartUpdate => ({
  type: "turn_start",
  turn_id: "turn-1",
  turn_index: 0,
  mode: "build",
  timestamp: "2026-06-10T00:00:00Z",
});

const toolCall = (
  overrides: Partial<WorkflowCopilotToolCallUpdate> = {},
): WorkflowCopilotToolCallUpdate => ({
  type: "tool_call",
  tool_name: "navigate_browser",
  tool_input: {},
  iteration: 0,
  tool_call_id: "call-1",
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
  timestamp: "2026-06-10T00:00:04Z",
  ...overrides,
});

const runOutcome = (
  overrides: Partial<WorkflowCopilotRunOutcomeUpdate> &
    Pick<WorkflowCopilotRunOutcomeUpdate, "verdict">,
): WorkflowCopilotRunOutcomeUpdate => ({
  type: "run_outcome",
  workflow_run_id: "wr_1",
  workflow_run_block_ids: ["wrb_block_1"],
  block_labels: ["block_1"],
  reason_code: null,
  display_reason: null,
  iteration: 0,
  timestamp: "2026-06-10T00:01:00Z",
  ...overrides,
});

const response = (
  overrides: Partial<WorkflowCopilotStreamResponseUpdate> = {},
): WorkflowCopilotStreamResponseUpdate => ({
  type: "response",
  workflow_copilot_chat_id: "chat-1",
  message: "Done.",
  response_time: "2026-06-10T00:02:00Z",
  proposal_disposition: "auto_applicable",
  ...overrides,
});

const phaseHint = (hintedAtMs: number): CopilotPhaseHintEvent => ({
  type: "client_phase_hint",
  hintedAtMs,
});

describe("applyNarrativeEvent — client_phase_hint", () => {
  it("sets draftingSignaledAt once", () => {
    const s = applyNarrativeEvent(EMPTY_NARRATIVE, phaseHint(5000));
    expect(s.draftingSignaledAt).toBe(5000);
  });

  it("is idempotent — a second hint never overwrites the first timestamp", () => {
    let s: TurnNarrativeState = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      phaseHint(5000),
    );
    s = applyNarrativeEvent(s, phaseHint(9000));
    expect(s.draftingSignaledAt).toBe(5000);
  });

  it("no-ops once a block has started running (design already progressed)", () => {
    let s: TurnNarrativeState = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      turnStart(),
    );
    s = applyNarrativeEvent(
      s,
      blockProgress({ block_label: "block_1", status: "running" }),
    );
    s = applyNarrativeEvent(s, phaseHint(5000));
    expect(s.draftingSignaledAt).toBeNull();
  });

  it("no-ops once designEnded", () => {
    let s: TurnNarrativeState = applyNarrativeEvent(EMPTY_NARRATIVE, {
      type: "design_end",
      timestamp: "2026-06-10T00:00:02Z",
    });
    s = applyNarrativeEvent(s, phaseHint(5000));
    expect(s.draftingSignaledAt).toBeNull();
  });

  it("no-ops once a draft already exists", () => {
    let s: TurnNarrativeState = applyNarrativeEvent(EMPTY_NARRATIVE, {
      type: "workflow_draft",
      block_count: 1,
      block_labels: ["block_1"],
      summary: null,
      timestamp: "2026-06-10T00:00:03Z",
    });
    s = applyNarrativeEvent(s, phaseHint(5000));
    expect(s.draftingSignaledAt).toBeNull();
  });
});

describe("applyNarrativeEvent — lastActivityAtMs", () => {
  it("stamps with the injected nowMs on tool_call/tool_result/narration", () => {
    const s = applyNarrativeEvent(EMPTY_NARRATIVE, toolCall(), 12345);
    expect(s.lastActivityAtMs).toBe(12345);
  });

  it("stamps even when the tool is denylisted from the visible activity log", () => {
    const s = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      toolCall({ tool_name: "get_run_results" }),
      777,
    );
    expect(s.lastActivityAtMs).toBe(777);
    expect(s.designActivity).toHaveLength(0);
  });

  it("defaults to Date.now() when nowMs is omitted", () => {
    const before = Date.now();
    const s = applyNarrativeEvent(EMPTY_NARRATIVE, toolCall());
    expect(s.lastActivityAtMs).toBeGreaterThanOrEqual(before);
  });
});

describe("applyNarrativeEvent — authoringCount", () => {
  it("increments only on AUTHORING_TOOLS tool_calls", () => {
    let s: TurnNarrativeState = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      toolCall({ tool_name: "navigate_browser" }),
    );
    expect(s.authoringCount).toBe(0);
    s = applyNarrativeEvent(s, toolCall({ tool_name: "update_workflow" }));
    expect(s.authoringCount).toBe(1);
    s = applyNarrativeEvent(
      s,
      toolCall({ tool_name: "update_and_run_blocks", tool_call_id: "call-2" }),
    );
    expect(s.authoringCount).toBe(2);
  });

  it("survives the MAX_DESIGN_ACTIVITY_ENTRIES eviction cap (does not reset when old entries scroll off)", () => {
    let s: TurnNarrativeState = EMPTY_NARRATIVE;
    s = applyNarrativeEvent(s, toolCall({ tool_name: "update_workflow" }));
    for (let i = 0; i < 60; i++) {
      s = applyNarrativeEvent(
        s,
        toolCall({ tool_name: "navigate_browser", tool_call_id: `c${i}` }),
      );
    }
    expect(s.authoringCount).toBe(1);
    expect(s.designActivity.length).toBeLessThan(61);
  });
});

describe("applyNarrativeEvent — lastRunOutcome", () => {
  it("run_outcome sets lastRunOutcome with the pre-event activitySeq", () => {
    let s: TurnNarrativeState = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      toolCall(),
    );
    s = applyNarrativeEvent(
      s,
      runOutcome({
        verdict: "not_demonstrated",
        display_reason: "outcome not confirmed",
      }),
    );
    expect(s.lastRunOutcome).toEqual({
      verdict: "not_demonstrated",
      role: "adjudicated",
      displayReason: "outcome not confirmed",
      activitySeqAtVerdict: 1,
    });
  });

  it("last-write-wins: an evaluating hold overwrites a stale failed verdict", () => {
    let s: TurnNarrativeState = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      runOutcome({ verdict: "not_demonstrated" }),
    );
    s = applyNarrativeEvent(s, runOutcome({ verdict: "evaluating" }));
    expect(s.lastRunOutcome?.verdict).toBe("evaluating");
  });
});

describe("applyNarrativeEvent — activitySeq (monotonic, cap-immune)", () => {
  it("increments on tool_call (agent-initiated steps)", () => {
    let s: TurnNarrativeState = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      toolCall(),
    );
    expect(s.activitySeq).toBe(1);
    s = applyNarrativeEvent(s, toolCall({ tool_call_id: "call-2" }));
    expect(s.activitySeq).toBe(2);
  });

  it("REGRESSION PIN: does NOT increment on tool_result — a failed run's own trailing tool_result must not look like new agent work (Codex catch)", () => {
    let s: TurnNarrativeState = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      toolCall(),
    );
    expect(s.activitySeq).toBe(1);
    s = applyNarrativeEvent(s, {
      type: "tool_result",
      tool_name: "navigate_browser",
      success: true,
      summary: "done",
      iteration: 0,
      tool_call_id: "call-1",
    });
    expect(s.activitySeq).toBe(1);
  });

  it("REGRESSION PIN: does NOT increment on narration — the narrator can report on a failed run's outcome independent of new revision work (Codex catch)", () => {
    let s: TurnNarrativeState = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      toolCall(),
    );
    expect(s.activitySeq).toBe(1);
    s = applyNarrativeEvent(s, {
      type: "narration",
      narration: "The run didn't confirm the goal was met.",
      iteration: 0,
      timestamp: "2026-06-10T00:00:00Z",
    });
    expect(s.activitySeq).toBe(1);
  });

  it("REGRESSION PIN: keeps incrementing past the MAX_DESIGN_ACTIVITY_ENTRIES cap, unlike designActivity.length", () => {
    let s: TurnNarrativeState = EMPTY_NARRATIVE;
    for (let i = 0; i < 55; i++) {
      s = applyNarrativeEvent(s, toolCall({ tool_call_id: `c${i}` }));
    }
    expect(s.designActivity.length).toBe(50);
    expect(s.activitySeq).toBe(55);
  });
});

describe("applyNarrativeEvent — response hydration resets phase-hint fields", () => {
  it("regression pin: a response frame for the SAME turnId preserves draftingSignaledAt (the terminal-wipe trap)", () => {
    let s: TurnNarrativeState = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      turnStart(),
    );
    s = applyNarrativeEvent(s, phaseHint(5000));
    expect(s.draftingSignaledAt).toBe(5000);

    s = applyNarrativeEvent(
      s,
      response({
        turn_id: "turn-1",
        narrative_payload: {
          turnId: "turn-1",
          turnIndex: 0,
          mode: "build",
          designStarted: true,
          designEnded: true,
          draft: null,
          blocks: [],
          terminal: "response",
          terminalMessage: "Done.",
          narrativeSummary: "Done.",
          priorBlockCount: null,
          designActivity: [],
          startedAt: "2026-06-10T00:00:00Z",
          endedAt: "2026-06-10T00:02:00Z",
        },
      }),
    );
    expect(s.draftingSignaledAt).toBe(5000);
  });

  it("no graft for a DIFFERENT turnId — fresh terminal payload starts at null", () => {
    let s: TurnNarrativeState = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      turnStart(),
    );
    s = applyNarrativeEvent(s, phaseHint(5000));

    s = applyNarrativeEvent(
      s,
      response({
        narrative_payload: {
          turnId: "turn-2",
          turnIndex: 1,
          mode: "build",
          designStarted: true,
          designEnded: true,
          draft: null,
          blocks: [],
          terminal: "response",
          terminalMessage: "Done.",
          narrativeSummary: "Done.",
          priorBlockCount: null,
          designActivity: [],
          startedAt: "2026-06-10T00:00:00Z",
          endedAt: "2026-06-10T00:02:00Z",
        },
      }),
    );
    expect(s.draftingSignaledAt).toBeNull();
  });

  it("authoringCount is grafted across the terminal swap (SKY-12969 parity); lastRunOutcome/activitySeq are not", () => {
    let s: TurnNarrativeState = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      turnStart(),
    );
    s = applyNarrativeEvent(s, toolCall({ tool_name: "update_workflow" }));
    s = applyNarrativeEvent(s, runOutcome({ verdict: "not_demonstrated" }));
    expect(s.authoringCount).toBe(1);
    expect(s.lastRunOutcome).not.toBeNull();

    s = applyNarrativeEvent(
      s,
      response({
        turn_id: "turn-1",
        narrative_payload: {
          turnId: "turn-1",
          turnIndex: 0,
          mode: "build",
          designStarted: true,
          designEnded: true,
          draft: null,
          blocks: [],
          terminal: "response",
          terminalMessage: "Done.",
          narrativeSummary: "Done.",
          priorBlockCount: null,
          designActivity: [],
          startedAt: "2026-06-10T00:00:00Z",
          endedAt: "2026-06-10T00:02:00Z",
        },
      }),
    );
    // Grafted (same turnId): the uncapped counter survives even though the
    // payload's capped designActivity has aged out the authoring entry, so
    // Explore stays resolved at the swap rather than flipping back.
    expect(s.authoringCount).toBe(1);
    // Still not grafted — a cancel-mid-redraft marks Test stopped, not Draft.
    expect(s.lastRunOutcome).toBeNull();
    expect(s.activitySeq).toBe(0);
  });
});

describe("applyNarrativeEvent — server-authored display labels", () => {
  const toolResult = (
    overrides: Partial<WorkflowCopilotToolResultUpdate> = {},
  ): WorkflowCopilotToolResultUpdate => ({
    type: "tool_result",
    tool_name: "edit_block",
    success: true,
    summary: "",
    iteration: 0,
    tool_call_id: "call-1",
    ...overrides,
  });

  it("renders a credential lookup label-only even when an older backend sends a count summary", () => {
    const s = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      toolResult({
        tool_name: "list_credentials",
        summary: "Found 4 credential(s)",
        success: true,
      }),
    );
    expect(s.designActivity).toHaveLength(1);
    expect(s.designActivity[0]!.text).toBe("Checking saved credentials");
  });

  it("keeps a credential blocker explanation that arrives as a successful summary", () => {
    const s = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      toolResult({
        tool_name: "list_credentials",
        summary:
          "Saved-credential scouting is not authorized for this request.",
        success: true,
      }),
    );
    expect(s.designActivity[0]!.text).toBe(
      "Saved-credential scouting is not authorized for this request.",
    );
  });

  it("still renders a credential lookup failure summary", () => {
    const s = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      toolResult({
        tool_name: "list_credentials",
        summary: "Failed: the credential store could not be reached",
        success: false,
      }),
    );
    expect(s.designActivity[0]!.text).toBe(
      "Failed: the credential store could not be reached",
    );
  });

  it("prefers the server display_label over the local map on tool_call and tool_result", () => {
    let s: TurnNarrativeState = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      toolCall({
        tool_name: "edit_block",
        display_label: 'Editing block "Log in"',
      }),
    );
    s = applyNarrativeEvent(
      s,
      toolResult({ display_label: 'Editing block "Log in"' }),
    );
    expect(s.designActivity.map((e) => e.displayLabel)).toEqual([
      'Editing block "Log in"',
      'Editing block "Log in"',
    ]);
    expect(s.designActivity.map((e) => e.text)).toEqual([
      'Editing block "Log in"…',
      'Editing block "Log in"',
    ]);
    expect(s.designActivity.every((e) => !e.text.includes("Working"))).toBe(
      true,
    );
  });

  it("falls back to the local label map when the server sends no display_label", () => {
    const s = applyNarrativeEvent(EMPTY_NARRATIVE, toolResult());
    expect(s.designActivity[0]?.displayLabel).toBe("Editing block");
  });

  it("renders a credential-lookup row instead of suppressing it", () => {
    let s: TurnNarrativeState = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      toolCall({ tool_name: "list_credentials", tool_call_id: "call-9" }),
    );
    s = applyNarrativeEvent(
      s,
      toolResult({ tool_name: "list_credentials", tool_call_id: "call-9" }),
    );
    expect(s.designActivity.map((e) => e.text)).toEqual([
      "Checking saved credentials…",
      "Checking saved credentials",
    ]);
  });

  it("keeps the persisted displayLabel when hydrating from narrative_payload", () => {
    const s = hydrateNarrativeFromPayload({
      turnId: "turn-1",
      turnIndex: 0,
      mode: "build",
      designStarted: true,
      designEnded: true,
      draft: null,
      blocks: [],
      terminal: "response",
      terminalMessage: "Done.",
      narrativeSummary: "Done.",
      priorBlockCount: null,
      designActivity: [
        {
          kind: "tool_call",
          text: 'Editing block "Log in"…',
          iteration: 0,
          toolName: "edit_block",
          displayLabel: 'Editing block "Log in"',
          id: "tc-call-1",
        },
        {
          kind: "tool_result",
          text: "Checking saved credentials",
          iteration: 1,
          toolName: "list_credentials",
          displayLabel: "Checking saved credentials",
          success: true,
          id: "tr-call-9",
        },
      ],
      startedAt: "2026-06-10T00:00:00Z",
      endedAt: "2026-06-10T00:02:00Z",
    });
    expect(s?.designActivity.map((e) => e.displayLabel)).toEqual([
      'Editing block "Log in"',
      "Checking saved credentials",
    ]);
  });
});
