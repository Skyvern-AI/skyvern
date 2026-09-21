import { describe, expect, it } from "vitest";

import {
  EMPTY_NARRATIVE,
  TurnNarrativeState,
  applyNarrativeEvent,
  hydrateNarrativeFromPayload,
} from "./narrativeState";
import {
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

describe("applyNarrativeEvent — designActivity", () => {
  it("keeps denylisted internal tools out of the visible activity log", () => {
    let s: TurnNarrativeState = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      toolCall({ tool_name: "get_run_results" }),
    );
    s = applyNarrativeEvent(s, {
      type: "tool_result",
      tool_name: "get_browser_screenshot",
      success: true,
      summary: "",
      iteration: 0,
      tool_call_id: "call-2",
    });
    expect(s.designActivity).toHaveLength(0);
  });

  it("evicts the oldest entries at the MAX_DESIGN_ACTIVITY_ENTRIES cap", () => {
    let s: TurnNarrativeState = EMPTY_NARRATIVE;
    for (let i = 0; i < 61; i++) {
      s = applyNarrativeEvent(
        s,
        toolCall({ tool_name: "navigate_browser", tool_call_id: `c${i}` }),
      );
    }
    expect(s.designActivity.length).toBeLessThan(61);
    expect(s.designActivity[s.designActivity.length - 1]?.id).toBe("tc-c60");
  });
});

describe("applyNarrativeEvent — lastRunOutcome", () => {
  it("run_outcome sets the latest factual run outcome", () => {
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
      role: "recorded",
      displayReason: "outcome not confirmed",
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

describe("applyNarrativeEvent — response hydration", () => {
  it("lastRunOutcome is not grafted across the terminal swap", () => {
    let s: TurnNarrativeState = applyNarrativeEvent(
      EMPTY_NARRATIVE,
      turnStart(),
    );
    s = applyNarrativeEvent(s, runOutcome({ verdict: "not_demonstrated" }));
    expect(s.lastRunOutcome).not.toBeNull();

    s = applyNarrativeEvent(
      s,
      response({
        turn_id: "turn-1",
        narrative_payload: {
          turnId: "turn-1",
          turnIndex: 0,
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
    expect(s.lastRunOutcome).toBeNull();
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
