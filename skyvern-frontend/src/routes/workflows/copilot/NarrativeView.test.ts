import { describe, expect, it } from "vitest";

import {
  BlockState,
  EMPTY_NARRATIVE,
  TurnNarrativeState,
  applyNarrativeEvent,
  computeTurnSummary,
  effectiveMode,
  hydrateHistoryNarrative,
  hydrateNarrativeFromPayload,
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

const summaryBlock = (
  label: string,
  state: BlockState["state"] = "completed",
): BlockState => ({
  workflowRunBlockId: `wrb_${label}`,
  label,
  blockType: "task",
  state,
  lastSeenIteration: 0,
  activity: [],
  startedAt: null,
  endedAt: null,
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
  mode: "build",
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

const buildTurn = (
  overrides: Partial<TurnNarrativeState> = {},
): TurnNarrativeState => ({
  ...EMPTY_NARRATIVE,
  mode: "build",
  terminal: "response",
  ...overrides,
});

const draft3 = {
  blockCount: 3,
  blockLabels: ["block_one", "block_two", "block_three"],
  summary: null,
};

describe("hydrateNarrativeFromPayload — terminal adjudication fields", () => {
  it("hydrates responseKind and verifiedSuccess from the payload", () => {
    const turn = hydrateNarrativeFromPayload(
      reproClarifyPayload({ responseKind: "build", verifiedSuccess: true }),
    );
    expect(turn?.responseKind).toBe("build");
    expect(turn?.verifiedSuccess).toBe(true);
  });

  it("treats absent fields as null", () => {
    const turn = hydrateNarrativeFromPayload(reproClarifyPayload());
    expect(turn?.responseKind).toBeNull();
    expect(turn?.verifiedSuccess).toBeNull();
  });

  it("treats unknown responseKind and non-boolean verifiedSuccess as absent", () => {
    const turn = hydrateNarrativeFromPayload(
      reproClarifyPayload({
        responseKind: "celebrate",
        verifiedSuccess: "yes",
      }),
    );
    expect(turn?.responseKind).toBeNull();
    expect(turn?.verifiedSuccess).toBeNull();
  });
});

describe("computeTurnSummary — typed terminal adjudication", () => {
  it("renders the loop-guard clarify repro as a Question, not a green built-and-tested claim", () => {
    const turn = hydrateNarrativeFromPayload(
      reproClarifyPayload({ responseKind: "clarify", verifiedSuccess: false }),
    );
    expect(turn).toBeDefined();
    const summary = computeTurnSummary(turn!);
    expect(summary.headline).toBe("Question");
    expect(summary.accent).toBe("qa");
    expect(summary.glyph).toBe("✦");
  });

  it("keeps the factual stats line on an adjudicated clarify build turn", () => {
    const turn = hydrateNarrativeFromPayload(
      reproClarifyPayload({ responseKind: "clarify", verifiedSuccess: false }),
    )!;
    const summary = computeTurnSummary(turn);
    expect(summary.stats).toEqual(["15:02", "3 blocks ran", "3 new"]);
  });

  it("never claims re-tested edits on an adjudicated clarify edit turn", () => {
    const turn = hydrateNarrativeFromPayload(
      reproClarifyPayload({
        responseKind: "clarify",
        verifiedSuccess: false,
        priorBlockCount: 2,
      }),
    )!;
    const summary = computeTurnSummary(turn);
    expect(summary.headline).toBe("Question");
    expect(summary.headline).not.toBe("Applied edits and re-tested");
    expect(summary.accent).not.toBe("ok");
  });

  it.each([
    ["clarify", "Question"],
    ["recover", "Question"],
    ["refuse", "Declined"],
    ["diagnose", "Answered"],
  ] as const)(
    "maps non-build kind %s to %s with qa accent",
    (kind, headline) => {
      const summary = computeTurnSummary(
        buildTurn({ responseKind: kind, verifiedSuccess: false }),
      );
      expect(summary.headline).toBe(headline);
      expect(summary.accent).toBe("qa");
      expect(summary.glyph).toBe("✦");
    },
  );

  it("renders a verdict-authorized tested success green with drafts", () => {
    const summary = computeTurnSummary(
      buildTurn({
        draft: draft3,
        proposalDisposition: "no_proposal",
        responseKind: "build",
        verifiedSuccess: true,
      }),
    );
    expect(summary.headline).toBe("Built and tested the workflow");
    expect(summary.accent).toBe("ok");
    expect(summary.glyph).toBe("✓");
  });

  it("renders a verdict-authorized edit turn green", () => {
    const summary = computeTurnSummary(
      buildTurn({
        draft: draft3,
        priorBlockCount: 2,
        responseKind: "build",
        verifiedSuccess: true,
      }),
    );
    expect(summary.headline).toBe("Applied edits and re-tested");
    expect(summary.accent).toBe("ok");
  });

  it("renders a verdict-authorized draftless turn as a completed run", () => {
    const summary = computeTurnSummary(
      buildTurn({ responseKind: "build", verifiedSuccess: true }),
    );
    expect(summary.headline).toBe("Completed the run");
    expect(summary.accent).toBe("ok");
  });

  it("ignores the prose question heuristic when a typed verdict authorizes success", () => {
    const summary = computeTurnSummary(
      buildTurn({
        draft: draft3,
        terminalMessage: "Could you provide feedback on the result?",
        responseKind: "build",
        verifiedSuccess: true,
      }),
    );
    expect(summary.headline).toBe("Built and tested the workflow");
    expect(summary.accent).toBe("ok");
  });

  it.each([
    [buildTurn({ responseKind: "build", verifiedSuccess: false }), "Stopped"],
    [
      buildTurn({
        draft: draft3,
        proposalDisposition: "auto_applicable",
        responseKind: "build",
        verifiedSuccess: false,
      }),
      "Stopped",
    ],
    [
      buildTurn({
        draft: draft3,
        priorBlockCount: 2,
        responseKind: "build",
        verifiedSuccess: false,
      }),
      "Stopped",
    ],
    [
      buildTurn({
        draft: draft3,
        proposalDisposition: "review_untested",
        responseKind: "build",
        verifiedSuccess: false,
      }),
      "Draft needs review",
    ],
    [
      buildTurn({
        draft: draft3,
        proposalDisposition: "review_tested",
        responseKind: "build",
        verifiedSuccess: false,
      }),
      "Workflow ready for review",
    ],
  ])(
    "never renders green when the verdict refused the success claim (%#)",
    (turn, headline) => {
      const summary = computeTurnSummary(turn);
      expect(summary.headline).toBe(headline);
      expect(summary.accent).toBe("qa");
      expect(summary.glyph).not.toBe("✓");
    },
  );

  it("a failed block still renders Run halted even with a verdict-authorized success", () => {
    const summary = computeTurnSummary(
      buildTurn({
        draft: draft3,
        blocks: [
          summaryBlock("block_one"),
          summaryBlock("block_two", "failed"),
        ],
        responseKind: "build",
        verifiedSuccess: true,
      }),
    );
    expect(summary.headline).toBe("Run halted");
    expect(summary.accent).toBe("fail");
    expect(summary.glyph).toBe("✕");
  });

  it("cancelled with a reviewable draft still renders Stopped with a draft", () => {
    const summary = computeTurnSummary(
      buildTurn({
        cancelled: true,
        draft: draft3,
        proposalDisposition: "review_untested",
        responseKind: "build",
        verifiedSuccess: true,
      }),
    );
    expect(summary.headline).toBe("Stopped with a draft");
    expect(summary.accent).toBe("qa");
  });

  it("legacy payload without adjudication renders via the unchanged inference chain", () => {
    const turn = hydrateNarrativeFromPayload(reproClarifyPayload())!;
    const summary = computeTurnSummary(turn);
    expect(summary.headline).toBe("Built and tested the workflow");
    expect(summary.accent).toBe("ok");
    expect(summary.stats).toEqual(["15:02", "3 blocks ran", "3 new"]);
  });
});

describe("hydrateHistoryNarrative — persisted turn_outcome graft", () => {
  it("grafts clarify from the adjacent turn_outcome onto a pre-fix payload", () => {
    const turn = hydrateHistoryNarrative(reproClarifyPayload(), {
      response_kind: "clarify",
    })!;
    expect(turn.responseKind).toBe("clarify");
    expect(turn.verifiedSuccess).toBeNull();
    const summary = computeTurnSummary(turn);
    expect(summary.headline).toBe("Question");
    expect(summary.accent).toBe("qa");
  });

  it("keeps the payload's own responseKind over the graft", () => {
    const turn = hydrateHistoryNarrative(
      reproClarifyPayload({ responseKind: "refuse", verifiedSuccess: false }),
      { response_kind: "clarify" },
    )!;
    expect(turn.responseKind).toBe("refuse");
  });

  it("renders grafted build-kind history via the legacy chain (no downgrade)", () => {
    const turn = hydrateHistoryNarrative(reproClarifyPayload(), {
      response_kind: "build",
    })!;
    expect(turn.responseKind).toBe("build");
    expect(turn.verifiedSuccess).toBeNull();
    const summary = computeTurnSummary(turn);
    expect(summary.headline).toBe("Built and tested the workflow");
    expect(summary.accent).toBe("ok");
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
          verifiedSuccess: false,
        }),
      }),
    );
    expect(s.responseKind).toBe("clarify");
    expect(s.verifiedSuccess).toBe(false);
    expect(computeTurnSummary(s).headline).toBe("Question");
  });

  it("leaves both fields null on frames from an older backend", () => {
    const s = applyNarrativeEvent(EMPTY_NARRATIVE, response());
    expect(s.responseKind).toBeNull();
    expect(s.verifiedSuccess).toBeNull();
  });
});
