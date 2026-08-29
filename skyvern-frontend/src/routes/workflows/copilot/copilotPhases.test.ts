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
  toolActivityDisplayLabel,
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

  it("edit_block_and_run is both authoring and a run tool", () => {
    expect(AUTHORING_TOOLS.has("edit_block_and_run")).toBe(true);
    expect(RUN_TOOLS.has("edit_block_and_run")).toBe(true);
  });

  it("run_blocks_and_collect_debug is a run tool only, not authoring", () => {
    expect(RUN_TOOLS.has("run_blocks_and_collect_debug")).toBe(true);
    expect(AUTHORING_TOOLS.has("run_blocks_and_collect_debug")).toBe(false);
  });
});

describe("toolActivityDisplayLabel — discovery tools (SKY-12385)", () => {
  it("labels discover_workflow_entrypoint and inspect_page_for_composition", () => {
    expect(toolActivityDisplayLabel("discover_workflow_entrypoint")).toBe(
      "Finding the entry page",
    );
    expect(toolActivityDisplayLabel("inspect_page_for_composition")).toBe(
      "Inspecting the page",
    );
  });

  it("labels fill_credential_field without naming any credential", () => {
    expect(toolActivityDisplayLabel("fill_credential_field")).toBe(
      "Entering saved credentials",
    );
  });

  it("still falls back to Working for unmapped tools", () => {
    expect(toolActivityDisplayLabel("some_unmapped_tool")).toBe("Working");
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

describe("derivePhases — composite scoped edit and run", () => {
  it("reaches Test on a terminal composite call even before block rows hydrate", () => {
    const rows = derivePhases(
      turn({
        designEnded: true,
        designActivity: [
          entry({
            id: "1",
            kind: "tool_call",
            toolName: "edit_block_and_run",
          }),
        ],
      }),
    );

    expect(phase(rows, "test").status).not.toBe("pending");
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

describe("derivePhases — condenses each phase's entries (SKY-11971)", () => {
  it("folds a failed-then-retried tool_call/tool_result pair in the explore bucket into one row", () => {
    const t = turn({
      designActivity: [
        entry({
          id: "tc-1",
          kind: "tool_call",
          toolName: "evaluate",
        }),
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "evaluate",
          success: false,
        }),
        entry({
          id: "tc-2",
          kind: "tool_call",
          toolName: "evaluate",
        }),
        entry({
          id: "tr-2",
          kind: "tool_result",
          toolName: "evaluate",
          success: true,
        }),
      ],
    });
    const rows = derivePhases(t);
    const exploreEntries = phase(rows, "explore").entries;
    expect(exploreEntries).toHaveLength(1);
    expect(exploreEntries[0]).toMatchObject({
      id: "tr-2",
      success: true,
      attempts: 2,
    });
  });

  it("does not fold a failed edit of one block into a later edit of a different block", () => {
    const t = turn({
      designActivity: [
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "edit_block",
          displayLabel: 'Editing block "Login Form"',
          success: false,
        }),
        entry({
          id: "tr-2",
          kind: "tool_result",
          toolName: "edit_block",
          displayLabel: 'Editing block "Search Box"',
          success: true,
        }),
      ],
    });
    const rows = derivePhases(t);
    const exploreEntries = phase(rows, "explore").entries;
    expect(exploreEntries).toHaveLength(2);
    expect(exploreEntries.some((e) => e.attempts !== undefined)).toBe(false);
  });

  it("REGRESSION PIN: the explore 'N steps' stub matches the condensed row count, not the raw event count (Claude catch)", () => {
    const t = turn({
      terminal: "response",
      designActivity: [
        entry({ id: "tc-1", kind: "tool_call", toolName: "evaluate" }),
        entry({
          id: "tr-1",
          kind: "tool_result",
          toolName: "evaluate",
          success: false,
        }),
        entry({ id: "tc-2", kind: "tool_call", toolName: "evaluate" }),
        entry({
          id: "tr-2",
          kind: "tool_result",
          toolName: "evaluate",
          success: true,
        }),
      ],
    });
    const rows = derivePhases(t);
    expect(phase(rows, "explore").entries).toHaveLength(1);
    expect(phase(rows, "explore").stub).toBe("1 step");
  });

  it("leaves the earlier bucket-routing test's un-paired hand-rolled ids unaffected", () => {
    // Regression pin: short test ids ("1"/"2"/"3"/"4") don't collide with
    // the tc-/tr- correlation convention, so condensing is a no-op there.
    const t = turn({
      designActivity: [
        entry({ id: "1", kind: "tool_call", toolName: "navigate_browser" }),
        entry({ id: "2", kind: "tool_call", toolName: "update_workflow" }),
      ],
      designEnded: true,
    });
    const rows = derivePhases(t);
    expect(phase(rows, "draft").entries.map((e) => e.id)).toEqual(["2"]);
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

  it("REGRESSION PIN (SKY-12969): the drafting-gap silence hint alone never completes Explore mid-scout", () => {
    // The 8s drafting-gap heuristic fires on any tool-call silence — including a
    // scout thinking-pause during login/2FA before the synthesis lane sets
    // turn.draft. A phase claim must derive from recorded authoring activity,
    // not the absence of frames, so a bare draftingSignaledAt leaves Explore
    // active and Draft pending.
    const t = turn({
      draftingSignaledAt: 1000,
      designActivity: [
        entry({ id: "1", kind: "tool_call", toolName: "navigate_browser" }),
      ],
    });
    const rows = derivePhases(t);
    expect(phase(rows, "explore").status).toBe("active");
    expect(phase(rows, "draft").status).toBe("pending");
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

  it("a deadline halt after a clean run stops the rail instead of failing it", () => {
    const t = turn({
      terminal: "error",
      designEnded: true,
      draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
      blocks: [block()],
      turnFacts: {
        factsAvailable: true,
        authoredBlockCount: 1,
        matchingSourceBlockCount: 1,
        evaluationState: "not_evaluated",
        runId: "wr_1",
        runCompleted: true,
        terminalCause: "deadline_expired",
        blocksRunThisTurn: 1,
        ranCleanOnCurrentSource: false,
      },
    });
    const rows = derivePhases(t);
    expect(phase(rows, "test").status).toBe("stopped");
    expect(phase(rows, "test").stub).toBe("1 block · time limit");
    expect(phase(rows, "done").status).toBe("stopped");
    expect(rows.map((r) => r.status)).not.toContain("fail");
  });

  it("a deadline halt with no block receipts never paints a green Test row", () => {
    const t = turn({
      terminal: "error",
      designEnded: true,
      draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
      designActivity: [
        entry({
          id: "1",
          kind: "tool_call",
          toolName: "update_and_run_blocks",
        }),
      ],
      turnFacts: {
        factsAvailable: true,
        authoredBlockCount: 1,
        matchingSourceBlockCount: 0,
        evaluationState: null,
        runId: null,
        runCompleted: null,
        terminalCause: "deadline_expired",
        blocksRunThisTurn: 0,
        ranCleanOnCurrentSource: false,
      },
    });
    const rows = derivePhases(t);
    expect(phase(rows, "test").status).toBe("stopped");
    expect(phase(rows, "test").stub).toBe("time limit");
  });

  it("a deadline halt with a failed block keeps the failure row", () => {
    const t = turn({
      terminal: "error",
      designEnded: true,
      draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
      blocks: [block({ state: "failed" })],
      turnFacts: {
        factsAvailable: true,
        authoredBlockCount: 1,
        matchingSourceBlockCount: 1,
        evaluationState: "not_demonstrated",
        runId: "wr_1",
        runCompleted: false,
        terminalCause: "deadline_expired",
        blocksRunThisTurn: 1,
        ranCleanOnCurrentSource: false,
      },
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

describe("derivePhases — SKY-12969 code-only incremental build (concurrent draft)", () => {
  // Mirrors the production session: scout tool completions interleaved with the
  // synthesis lane's offer renders (turn.draft) at trajectory 1 and 4, zero
  // AUTHORING_TOOLS entries, user-cancel terminal.
  const OFFER = { blockCount: 1, blockLabels: ["block_1"], summary: null };
  const scoutTools = [
    "navigate_browser",
    "evaluate",
    "evaluate",
    "take_screenshot",
    "evaluate",
    "fill_credential_field",
    "fill_credential_field",
    "evaluate",
    "click",
    "fill_credential_field",
    "click",
  ];
  const scout = (n: number): ActivityEntry[] =>
    scoutTools
      .slice(0, n)
      .map((toolName, i) =>
        entry({ id: `s${i}`, kind: "tool_call", toolName }),
      );

  it("AC1: Explore stays active (never done) while the scout is mid-login and an offer has already rendered", () => {
    // Screenshot-1 instant: 10 scout completions, offer rendered (turn.draft set
    // by the synthesis lane), no authoring tool, live browser still on 2FA.
    const midScout = turn({
      terminal: null,
      draft: OFFER,
      designActivity: scout(10),
    });
    const rows = derivePhases(midScout);
    expect(phase(rows, "explore").status).toBe("active");
    expect(phase(rows, "draft").status).toBe("pending");
    // An active Explore row shows no done-stub — its count cannot be claimed
    // complete while it is still growing.
    expect(phase(rows, "explore").stub).toBeNull();
  });

  it("AC1: the growing step count never lands under a completed row", () => {
    const at10 = derivePhases(
      turn({ terminal: null, draft: OFFER, designActivity: scout(10) }),
    );
    const at11 = derivePhases(
      turn({ terminal: null, draft: OFFER, designActivity: scout(11) }),
    );
    expect(phase(at10, "explore").status).toBe("active");
    expect(phase(at11, "explore").status).toBe("active");
    expect(phase(at11, "explore").entries.length).toBeGreaterThan(
      phase(at10, "explore").entries.length,
    );
  });

  it("AC1: a user-cancel mid-login marks Explore stopped (not done); Draft stays pending", () => {
    const cancelled = turn({
      terminal: "response",
      cancelled: true,
      designEnded: true,
      draft: OFFER,
      designActivity: scout(11),
    });
    const rows = derivePhases(cancelled);
    expect(phase(rows, "explore").status).toBe("stopped");
    expect(phase(rows, "draft").status).toBe("pending");
    expect(phase(rows, "done").status).toBe("stopped");
  });

  it("AC1/ruling: a healthy (non-cancelled) response terminal carrying the proposal resolves the phases done, count frozen", () => {
    // The user let the same turn finish: scout completed, proposal offered, no
    // authoring tool, no run. The turn is over — Explore genuinely ended, so the
    // row completes WITH its final count (count-immutability holds).
    const healthy = turn({
      terminal: "response",
      cancelled: false,
      designEnded: true,
      draft: OFFER,
      designActivity: scout(11),
    });
    const rows = derivePhases(healthy);
    expect(phase(rows, "explore").status).toBe("done");
    expect(phase(rows, "draft").status).toBe("done");
    expect(phase(rows, "test").status).toBe("notrun");
    expect(phase(rows, "done").status).toBe("done");
    expect(phase(rows, "explore").stub).toBe("11 steps");
  });

  it("AC1: hydration parity — a draft-only authoring turn resolves identically live and after reload", () => {
    // authoringCount is client-only and resets to 0 on a history reload; the
    // hydrated designActivity still carries the update_workflow entry, so
    // authoringSeen keeps Explore/Draft resolved to done both ways (no jump).
    const authored = [
      entry({ id: "1", kind: "tool_call", toolName: "navigate_browser" }),
      entry({ id: "2", kind: "tool_call", toolName: "update_workflow" }),
    ];
    const live = turn({
      terminal: "response",
      designEnded: true,
      authoringCount: 1,
      draft: OFFER,
      designActivity: authored,
    });
    const hydrated = turn({
      terminal: "response",
      designEnded: true,
      authoringCount: 0,
      draft: OFFER,
      designActivity: authored,
    });
    expect(derivePhases(hydrated).map((r) => r.status)).toEqual(
      derivePhases(live).map((r) => r.status),
    );
    expect(phase(derivePhases(hydrated), "explore").status).toBe("done");
    expect(phase(derivePhases(hydrated), "draft").status).toBe("done");
  });

  it("AC1/parity: a cancelled terminal with authoring evidence but an aged-out activity list still resolves Explore done, Draft stopped", () => {
    // The reducer grafts the uncapped authoringCount across the terminal swap;
    // the derivation must honor it so an authoring turn whose sole authoring
    // entry evicted from the 50-capped activity list doesn't flip Explore back
    // to stopped after the swap (SKY-11970 live/terminal parity).
    const cancelledEvicted = turn({
      terminal: "response",
      cancelled: true,
      designEnded: true,
      authoringCount: 1,
      draft: OFFER,
      designActivity: [],
    });
    const rows = derivePhases(cancelledEvicted);
    expect(phase(rows, "explore").status).toBe("done");
    expect(phase(rows, "draft").status).toBe("stopped");
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

describe("derivePhases — test-run count on the not-confirmed stub (SKY-11339)", () => {
  const notConfirmedTurn = (
    runToolNames: string[],
    outcomeRole?: BlockState["outcomeRole"],
  ): TurnNarrativeState =>
    turn({
      terminal: "response",
      designEnded: true,
      draft: { blockCount: 1, blockLabels: ["block_1"], summary: null },
      blocks: [
        block({
          state: "completed",
          outcome: "not_demonstrated",
          outcomeRole,
        }),
      ],
      designActivity: runToolNames.map((toolName, i) =>
        entry({ id: `r${i}`, kind: "tool_call", toolName }),
      ),
    });

  it("surfaces the run count when the workflow was tested multiple times", () => {
    const rows = derivePhases(
      notConfirmedTurn([
        "update_and_run_blocks",
        "update_and_run_blocks",
        "run_blocks_and_collect_debug",
      ]),
    );
    expect(phase(rows, "test").stub).toBe("3 runs · not confirmed");
  });

  it("omits the count for a single run, keeping today's look", () => {
    const rows = derivePhases(notConfirmedTurn(["update_and_run_blocks"]));
    expect(phase(rows, "test").stub).toBe("· not confirmed");
  });

  it("keeps an explicitly adjudicated negative outcome in the alarm stub", () => {
    const rows = derivePhases(
      notConfirmedTurn(["update_and_run_blocks"], "adjudicated"),
    );
    expect(phase(rows, "test").stub).toBe("· not confirmed");
  });

  it("suppresses the alarm stub for an interim build test", () => {
    const rows = derivePhases(
      notConfirmedTurn(["update_and_run_blocks"], "interim_build_test"),
    );
    expect(phase(rows, "test").stub).not.toContain("not confirmed");
  });
});
