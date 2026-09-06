import { describe, expect, it } from "vitest";

import bundles from "./narrativeState.turnFacts.fixture.json";
import { getReviewGateVerdict } from "./cards/ReviewGateCard";
import {
  EMPTY_NARRATIVE,
  TurnNarrativeState,
  TerminalEnvelopeFacts,
  applyNarrativeEvent,
  awaitsUserInput,
  hydrateNarrativeFromPayload,
  hydrateHistoryNarrative,
  isBlockOk,
  isDeadlineHalt,
  notConfirmedOutcome,
  ranCleanOnCurrentSource,
} from "./narrativeState";

import {
  WorkflowCopilotBlockProgressUpdate,
  WorkflowCopilotRunOutcomeUpdate,
  WorkflowCopilotStreamErrorUpdate,
  WorkflowCopilotStreamResponseUpdate,
  WorkflowCopilotTurnStartUpdate,
} from "./workflowCopilotTypes";

describe("budget expiry narrative", () => {
  it("hydrates typed status from the terminal payload", () => {
    const turn = hydrateNarrativeFromPayload({
      turnId: "turn-1",
      terminal: "error",
      budgetExpiry: {
        budgetExpired: true,
        source: "deadline",
        reportProduced: false,
        stagedDraftId: "wf_draft",
        drainFingerprint: "drain-1",
      },
    });
    expect(turn?.budgetExpiry).toEqual({
      budgetExpired: true,
      source: "deadline",
      reportProduced: false,
      stagedDraftId: "wf_draft",
      drainFingerprint: "drain-1",
    });
  });

  it("grafts persisted expiry facts when the narrative predates them", () => {
    const turn = hydrateHistoryNarrative(
      { turnId: "turn-1", terminal: "error" },
      {
        budget_expired: true,
        budget_expiry_source: "max_turns",
        budget_expiry_report_produced: false,
        budget_expiry_staged_draft_id: "wf_draft",
        drain_fingerprint: "drain-2",
      },
    );
    expect(turn?.budgetExpiry?.source).toBe("max_turns");
    expect(turn?.budgetExpiry?.drainFingerprint).toBe("drain-2");
  });
});

const turnStart = (): WorkflowCopilotTurnStartUpdate => ({
  type: "turn_start",
  turn_id: "turn-1",
  turn_index: 0,
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

const envelopeFacts = (
  facts: Partial<TerminalEnvelopeFacts>,
): TerminalEnvelopeFacts => ({
  nextState: null,
  renderedFromEnvelope: false,
  runVerdict: null,
  runDisplayReason: null,
  runId: null,
  ...facts,
});

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
      expect(b.outcomeRole).toBe("recorded");
      expect(isBlockOk(b)).toBe(false);
    }
    expect(s.lastRunOutcome?.role).toBe("recorded");
    expect(notConfirmedOutcome(s)?.verdict).toBe("not_demonstrated");
  });

  it("interim verdict stays non-green but suppresses live not-confirmed alarms", () => {
    const s = reduce([
      ...bothBlocksRan,
      runOutcome({
        verdict: "not_demonstrated",
        role: "interim_build_test",
        reason_code: "no_meaningful_output",
        display_reason: "The workflow still needs its extraction block.",
      }),
    ]);

    for (const b of s.blocks) {
      expect(b.outcomeRole).toBe("interim_build_test");
      expect(isBlockOk(b)).toBe(false);
    }
    expect(s.lastRunOutcome?.role).toBe("interim_build_test");
    expect(notConfirmedOutcome(s)).toBeNull();
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
    terminal: "response",
    terminalMessage: "Done.",
    startedAt: "2026-06-10T00:00:00Z",
    endedAt: "2026-06-10T00:02:00Z",
    blocks,
  });

  it("round-trips outcome/outcomeReason/outcomeRole so reload renders like the live stream", () => {
    const live = reduce([
      turnStart(),
      blockProgress({ block_label: "search_person", status: "running" }),
      blockProgress({ block_label: "search_person", status: "completed" }),
      runOutcome({
        verdict: "not_demonstrated",
        role: "interim_build_test",
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
          outcomeRole: "interim_build_test",
        }),
      ]),
    )!;
    const hydratedRow = hydrated.blocks[0]!;

    expect(hydratedRow.state).toBe(liveRow.state);
    expect(hydratedRow.outcome).toBe(liveRow.outcome);
    expect(hydratedRow.outcomeReason).toBe(liveRow.outcomeReason);
    expect(hydratedRow.outcomeRole).toBe(liveRow.outcomeRole);
    expect(isBlockOk(hydratedRow)).toBe(false);
    expect(isBlockOk(liveRow)).toBe(false);
  });

  it("round-trips a recorded outcome role across reload", () => {
    const hydrated = hydrateNarrativeFromPayload(
      payload([
        payloadBlock({
          outcome: "not_evaluated",
          outcomeRole: "recorded",
        }),
      ]),
    )!;

    expect(hydrated.blocks[0]?.outcomeRole).toBe("recorded");
  });

  it("hydrates rows without outcome keys exactly as before", () => {
    const hydrated = hydrateNarrativeFromPayload(payload([payloadBlock({})]))!;
    const row = hydrated.blocks[0]!;
    expect(row.outcome).toBeUndefined();
    expect(row.outcomeReason).toBeUndefined();
    expect(row.outcomeRole).toBeUndefined();
    expect(isBlockOk(row)).toBe(true);
  });

  it("ignores unknown outcome values", () => {
    const hydrated = hydrateNarrativeFromPayload(
      payload([payloadBlock({ outcome: "maybe", outcomeReason: 7 })]),
    )!;
    const row = hydrated.blocks[0]!;
    expect(row.outcome).toBeUndefined();
    expect(row.outcomeReason).toBeUndefined();
    expect(row.outcomeRole).toBeUndefined();
    expect(isBlockOk(row)).toBe(true);
  });

  it("normalizes absent and unknown persisted roles to adjudicated", () => {
    const hydrated = hydrateNarrativeFromPayload(
      payload([
        payloadBlock({ outcome: "not_demonstrated" }),
        payloadBlock({
          label: "search_person_2",
          outcome: "not_demonstrated",
          outcomeRole: "future_role",
        }),
      ]),
    )!;

    expect(hydrated.blocks.map((block) => block.outcomeRole)).toEqual([
      "adjudicated",
      "adjudicated",
    ]);
    expect(notConfirmedOutcome(hydrated)?.verdict).toBe("not_demonstrated");
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

  it("hydrates terminal envelope run facts and leaves legacy rows null", () => {
    const withEnvelope = hydrateNarrativeFromPayload({
      ...payload([]),
      terminalEnvelope: {
        next_state: "stopped",
        verified: false,
        workflow_applied: false,
        run_verdict: "not_demonstrated",
        run_display_reason: "Checkout never reached confirmation.",
        response_kind: "stopped",
        envelope_version: 1,
      },
    })!;
    expect(withEnvelope.terminalEnvelope).toEqual({
      nextState: "stopped",
      renderedFromEnvelope: false,
      runVerdict: "not_demonstrated",
      runDisplayReason: "Checkout never reached confirmation.",
      runId: null,
      runOutcomeRole: null,
      connectFailure: null,
    });

    const question = hydrateNarrativeFromPayload({
      ...payload([]),
      terminalEnvelope: {
        next_state: "awaiting_user_input",
        response_kind: "question",
        user_action_required: true,
        rendered_from_envelope: true,
      },
    })!;
    expect(question.terminalEnvelope).toEqual({
      nextState: "awaiting_user_input",
      renderedFromEnvelope: true,
      runVerdict: null,
      runDisplayReason: null,
      runId: null,
      runOutcomeRole: null,
      connectFailure: null,
    });
    expect(awaitsUserInput(question)).toBe(true);
    // Unstamped envelope: carried, but not display authority.
    expect(
      awaitsUserInput({
        cancelled: false,
        terminalEnvelope: {
          ...question.terminalEnvelope!,
          renderedFromEnvelope: false,
        },
      }),
    ).toBe(false);
    // The cancel path persists this envelope verbatim, so the stop has to win.
    expect(
      awaitsUserInput({
        cancelled: true,
        terminalEnvelope: question.terminalEnvelope,
      }),
    ).toBe(false);

    const legacy = hydrateNarrativeFromPayload(payload([]))!;
    expect(legacy.terminalEnvelope).toBeNull();

    const malformed = hydrateNarrativeFromPayload({
      ...payload([]),
      terminalEnvelope: { run_verdict: "maybe", run_display_reason: 7 },
    })!;
    expect(malformed.terminalEnvelope).toEqual({
      nextState: null,
      renderedFromEnvelope: false,
      runVerdict: null,
      runDisplayReason: null,
      runId: null,
      runOutcomeRole: null,
      connectFailure: null,
    });

    const typedConnect = hydrateNarrativeFromPayload({
      ...payload([]),
      terminalEnvelope: {
        connect_failure: {
          state: "already_closed",
          retry_action: "test_end_to_end",
          browser_session_id: "pbs_1",
        },
      },
    })!;
    expect(typedConnect.terminalEnvelope?.connectFailure).toEqual({
      state: "already_closed",
      retryAction: "test_end_to_end",
      workflowRunId: null,
      workflowRunBlockId: null,
      taskId: null,
      browserSessionId: "pbs_1",
    });

    const futureConnectState = hydrateNarrativeFromPayload({
      ...payload([]),
      terminalEnvelope: {
        connect_failure: {
          state: "future_manager_state",
          retry_action: "test_end_to_end",
          browser_session_id: "pbs_2",
        },
      },
    })!;
    expect(futureConnectState.terminalEnvelope?.connectFailure).toBeNull();

    const missingRetryContract = hydrateNarrativeFromPayload({
      ...payload([]),
      terminalEnvelope: {
        connect_failure: {
          state: "already_closed",
          browser_session_id: "pbs_3",
        },
      },
    })!;
    expect(missingRetryContract.terminalEnvelope?.connectFailure).toBeNull();
  });
});

describe("notConfirmedOutcome — envelope-first", () => {
  const base = {
    ...EMPTY_NARRATIVE,
    lastRunOutcome: null,
    blocks: [],
  };

  it("envelope not_demonstrated wins even when a later pointer says demonstrated", () => {
    const outcome = notConfirmedOutcome({
      ...base,
      terminalEnvelope: envelopeFacts({
        runVerdict: "not_demonstrated",
        runDisplayReason: "Cart never showed the item.",
      }),
      lastRunOutcome: {
        verdict: "demonstrated",
        displayReason: null,
      },
    });
    expect(outcome).toEqual({
      verdict: "not_demonstrated",
      displayReason: "Cart never showed the item.",
    });
  });

  it("envelope demonstrated suppresses the block-derived not_demonstrated", () => {
    const outcome = notConfirmedOutcome({
      ...base,
      terminalEnvelope: envelopeFacts({ runVerdict: "demonstrated" }),
      blocks: [
        {
          workflowRunBlockId: "wrb_1",
          label: "checkout",
          blockType: "code",
          outcome: "not_demonstrated",
          outcomeReason: "stale",
          state: "completed",
          lastSeenIteration: 0,
          activity: [],
          startedAt: null,
          endedAt: null,
        },
      ],
    });
    expect(outcome).toBeNull();
  });

  it("envelope not_evaluated suppresses not-confirmed like demonstrated does", () => {
    const outcome = notConfirmedOutcome({
      ...base,
      terminalEnvelope: envelopeFacts({ runVerdict: "not_evaluated" }),
      lastRunOutcome: {
        verdict: "not_demonstrated",
        displayReason: "Stale pointer.",
      },
    });
    expect(outcome).toBeNull();
  });

  it("an interim run-start envelope does not suppress the recorded not-confirmed outcome", () => {
    const outcome = notConfirmedOutcome({
      ...base,
      terminalEnvelope: envelopeFacts({
        runVerdict: "not_evaluated",
        runOutcomeRole: "interim_build_test",
      }),
      lastRunOutcome: {
        verdict: "not_demonstrated",
        displayReason: "The earlier run never reached the dashboard.",
      },
    });
    expect(outcome).toEqual({
      verdict: "not_demonstrated",
      displayReason: "The earlier run never reached the dashboard.",
    });
  });

  it("envelope without a run verdict falls back to the legacy inference", () => {
    const outcome = notConfirmedOutcome({
      ...base,
      terminalEnvelope: envelopeFacts({}),
      lastRunOutcome: {
        verdict: "not_demonstrated",
        displayReason: "Legacy pointer reason.",
      },
    });
    expect(outcome).toEqual({
      verdict: "not_demonstrated",
      displayReason: "Legacy pointer reason.",
    });
  });
});

// A recorded terminal-surface packet: one authored block, one clean run against
// the staged source.
const CLEAN_RECORDED_TURN = "surface-20260820T082523349904";

describe("canonical turn facts — pill and prose over one bundle", () => {
  const surfaces = (leg: keyof typeof bundles) => {
    const turn = hydrateNarrativeFromPayload(
      bundles[leg] as unknown as Record<string, unknown>,
    );
    if (!turn) throw new Error(`fixture ${leg} did not hydrate`);
    return {
      turn,
      pill: getReviewGateVerdict(turn, null),
      prose: turn.terminalMessage ?? "",
    };
  };

  it("states coverage and evaluation as independent facts when one of three blocks ran", () => {
    const { turn, pill, prose } = surfaces("partial-coverage");

    expect(turn.turnFacts?.authoredBlockCount).toBe(3);
    expect(turn.turnFacts?.matchingSourceBlockCount).toBe(1);
    expect(turn.turnFacts?.evaluationState).toBe("not_evaluated");
    expect(pill).toBe("untested");
    expect(prose).not.toContain("tested draft");
  });

  it("reports a deadline halt without a failure row or a tested pill", () => {
    const { turn, pill, prose } = surfaces("deadline-after-one-clean-run");

    expect(turn.turnFacts?.terminalCause).toBe("deadline_expired");
    expect(turn.blocks.some((block) => block.state === "failed")).toBe(false);
    expect(isDeadlineHalt(turn)).toBe(true);
    expect(pill).toBe("untested");
    expect(prose).toContain("1 block ran this turn");
    expect(prose).toContain("outcome was not evaluated");
    expect(prose).toContain("reached its time limit");
  });

  it("marks an edited block as run against different source, never as failed", () => {
    const { turn, pill } = surfaces("different-source-edit-one-of-two");

    expect(
      turn.review?.blocks.map((block) => [block.label, block.coverage]),
    ).toEqual([
      ["extract_github_star_count", "different_source"],
      ["append_star_count_to_sheet", "never_run"],
      ["append_star_count_to_sales_marketing", "never_run"],
    ]);
    expect(turn.blocks.some((block) => block.state === "failed")).toBe(false);
    expect(pill).toBe("untested");
  });

  it("names clean lifecycle plus full current-source coverage on the satisfaction path", () => {
    const { turn, pill, prose } = surfaces("satisfaction");

    expect(turn.turnFacts?.authoredBlockCount).toBe(3);
    expect(turn.turnFacts?.matchingSourceBlockCount).toBe(3);
    expect(turn.turnFacts?.runCompleted).toBe(true);
    expect(ranCleanOnCurrentSource(turn.turnFacts)).toBe(true);
    expect(pill).toBe("tested");
    expect(prose).not.toMatch(/verified|demonstrated/i);
  });

  it("states its counts without a completion claim when no run is anchored to the turn", () => {
    const { turn } = surfaces(CLEAN_RECORDED_TURN);

    expect(turn.blocks.map((block) => block.state)).toEqual(["completed"]);
    expect(turn.turnFacts?.runCompleted).toBeNull();
    expect(turn.turnFacts?.authoredBlockCount).toBe(1);
    expect(turn.turnFacts?.matchingSourceBlockCount).toBe(1);
  });

  it("withholds the tested pill when the recorded run did not complete", () => {
    const recorded = bundles[CLEAN_RECORDED_TURN] as unknown as {
      turnFacts: Record<string, unknown>;
    };
    const turn = hydrateNarrativeFromPayload({
      ...(bundles[CLEAN_RECORDED_TURN] as unknown as Record<string, unknown>),
      turnFacts: {
        ...recorded.turnFacts,
        runCompleted: false,
        ranCleanOnCurrentSource: false,
      },
    });
    if (!turn) throw new Error("fixture did not hydrate");

    expect(turn.turnFacts?.runCompleted).toBe(false);
    expect(ranCleanOnCurrentSource(turn.turnFacts)).toBe(false);
    expect(getReviewGateVerdict(turn, null)).toBe("untested");
  });

  it("makes no completion claim when a stop left the run start unresolved", () => {
    const recorded = bundles[CLEAN_RECORDED_TURN] as unknown as {
      turnFacts: Record<string, unknown>;
    };
    const turn = hydrateNarrativeFromPayload({
      ...(bundles[CLEAN_RECORDED_TURN] as unknown as Record<string, unknown>),
      turnFacts: {
        ...recorded.turnFacts,
        runId: "wr_1",
        runCompleted: null,
        blocksRunThisTurn: null,
        evaluationState: null,
        ranCleanOnCurrentSource: false,
      },
    });
    if (!turn) throw new Error("fixture did not hydrate");

    expect(turn.turnFacts?.runCompleted).toBeNull();
    expect(turn.turnFacts?.authoredBlockCount).toBe(1);
    expect(turn.turnFacts?.matchingSourceBlockCount).toBe(1);
    expect(ranCleanOnCurrentSource(turn.turnFacts)).toBe(false);
    expect(getReviewGateVerdict(turn, null)).toBe("untested");
  });

  it("says a renamed block is untested under this name across review and pill", () => {
    const { turn, pill } = surfaces("renamed-block");

    expect(
      turn.review?.blocks.map((block) => [block.label, block.coverage]),
    ).toEqual([
      ["extract_github_star_count_v2", "unknown"],
      ["append_star_count_to_sheet", "current_source"],
      ["append_star_count_to_sales_marketing", "current_source"],
      ["extract_github_star_count", undefined],
    ]);
    expect(turn.turnFacts?.matchingSourceBlockCount).toBe(2);
    expect(pill).toBe("untested");
  });

  it("settles a block still running at the deadline as stopped, not failed", () => {
    const turn = hydrateNarrativeFromPayload({
      ...(bundles["deadline-after-one-clean-run"] as unknown as Record<
        string,
        unknown
      >),
      blocks: [
        {
          workflowRunBlockId: "wrb_1",
          label: "append_star_count_to_sheet",
          blockType: "code",
          state: "running",
          lastSeenIteration: 0,
          activity: [],
        },
      ],
    });
    if (!turn) throw new Error("fixture did not hydrate");

    expect(turn.blocks.map((block) => block.state)).toEqual(["stopped"]);
    expect(isDeadlineHalt(turn)).toBe(true);
  });

  it("keeps a failed block a failure even when the deadline expired", () => {
    const turn = hydrateNarrativeFromPayload({
      ...(bundles["deadline-after-one-clean-run"] as unknown as Record<
        string,
        unknown
      >),
      blocks: [
        {
          workflowRunBlockId: "wrb_1",
          label: "append_star_count_to_sheet",
          blockType: "code",
          state: "failed",
          lastSeenIteration: 0,
          activity: [],
        },
      ],
    });
    if (!turn) throw new Error("fixture did not hydrate");

    expect(turn.turnFacts?.terminalCause).toBe("deadline_expired");
    expect(isDeadlineHalt(turn)).toBe(false);
  });

  it("makes no tested claim for a turn that published no facts or coverage", () => {
    const legacy = bundles[CLEAN_RECORDED_TURN] as unknown as Record<
      string,
      unknown
    >;
    const review = legacy.review as { blocks: Array<Record<string, unknown>> };
    const turn = hydrateNarrativeFromPayload({
      ...legacy,
      turnFacts: undefined,
      review: {
        ...review,
        blocks: review.blocks.map((block) => {
          const withoutCoverage = { ...block };
          delete withoutCoverage.coverage;
          return withoutCoverage;
        }),
      },
    });
    if (!turn) throw new Error("fixture did not hydrate");

    expect(turn.turnFacts).toBeNull();
    expect(turn.review?.blocks[0]?.coverage).toBeUndefined();
    expect(getReviewGateVerdict(turn, null)).toBe("untested");
  });

  it("keeps the review when the backend sends a coverage value it does not know", () => {
    const known = bundles[CLEAN_RECORDED_TURN] as unknown as Record<
      string,
      unknown
    >;
    const review = known.review as { blocks: Array<Record<string, unknown>> };
    const turn = hydrateNarrativeFromPayload({
      ...known,
      review: {
        ...review,
        blocks: review.blocks.map((block) => ({
          ...block,
          coverage: "some_future_value",
        })),
      },
    });
    if (!turn) throw new Error("fixture did not hydrate");

    expect(turn.review?.blocks).toHaveLength(1);
    expect(turn.review?.blocks[0]?.coverage).toBeUndefined();
  });

  it("refuses a tested pill for a pre-fix payload that carries no fact bundle", () => {
    const turn = hydrateNarrativeFromPayload(
      bundles["facts-absent"] as unknown as Record<string, unknown>,
    );
    if (!turn) throw new Error("fixture did not hydrate");

    expect(turn.turnFacts).toBeNull();
    expect(turn.proposalDisposition).toBe("review_tested");
    expect(getReviewGateVerdict(turn, null)).toBe("untested");
  });

  it("keeps facts and pill telling one story when the envelope is missing", () => {
    const full = bundles["full-coverage"] as unknown as Record<string, unknown>;
    const facts = full.turnFacts as Record<string, unknown>;
    const turn = hydrateNarrativeFromPayload({
      ...full,
      turnFacts: {
        ...facts,
        evaluationState: null,
        runId: null,
        runCompleted: null,
        terminalCause: null,
        blocksRunThisTurn: null,
      },
    });
    if (!turn) throw new Error("fixture did not hydrate");

    expect(turn.turnFacts?.factsAvailable).toBe(true);
    expect(turn.turnFacts?.authoredBlockCount).toBe(facts.authoredBlockCount);
    expect(turn.turnFacts?.matchingSourceBlockCount).toBe(
      facts.matchingSourceBlockCount,
    );
    expect(turn.turnFacts?.runCompleted).toBeNull();
    expect(ranCleanOnCurrentSource(turn.turnFacts)).toBe(true);
    expect(getReviewGateVerdict(turn, null)).toBe("tested");
  });

  it("stays non-committal when the turn published no facts", () => {
    const turn = hydrateNarrativeFromPayload({
      ...(bundles["full-coverage"] as unknown as Record<string, unknown>),
      review: undefined,
      proposalDisposition: "auto_applicable",
      turnFacts: {
        factsAvailable: false,
        evaluationState: null,
        runId: null,
        terminalCause: null,
        blocksRunThisTurn: null,
      },
    });
    if (!turn) throw new Error("fixture did not hydrate");

    expect(ranCleanOnCurrentSource(turn.turnFacts)).toBe(false);
    expect(getReviewGateVerdict(turn, null)).toBe("untested");
  });
});

describe("hydrateNarrativeFromPayload -- a superseded build test", () => {
  const BACKEND_AUTHORED_REASON =
    "Stopped because a newer test in this chat took over the browser.";

  it("shows the backend's run reason back verbatim after a reload", () => {
    const hydrated = hydrateNarrativeFromPayload({
      turnId: "turn-1",
      turnIndex: 0,
      terminal: "response",
      blocks: [],
      terminalEnvelope: {
        run_verdict: "not_demonstrated",
        run_display_reason: BACKEND_AUTHORED_REASON,
        terminal_cause: "occupied",
      },
    });

    expect(hydrated).toBeDefined();
    const outcome = notConfirmedOutcome(hydrated!);
    expect(outcome?.verdict).toBe("not_demonstrated");
    expect(outcome?.displayReason).toBe(BACKEND_AUTHORED_REASON);
    expect(outcome?.displayReason).not.toBe("Cancelled by user.");
  });
});
