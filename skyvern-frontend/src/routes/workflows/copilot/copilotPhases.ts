// Pure derivation of the 4-phase build checklist (Explore / Draft / Test-run
// / Done) from TurnNarrativeState. Kept dependency-light and side-effect-free
// (mirrors actionReveal.ts) so the same rule produces identical rows live,
// at the terminal payload swap, and on a hydrated history reload — the
// reducer's `response` case REPLACES client state with the BE payload, so any
// live-only derivation would make rows visibly jump at that instant.
import {
  ActivityEntry,
  AUTHORING_TOOLS,
  BlockState,
  RUN_TOOLS,
  TurnNarrativeState,
  condenseActivityEntries,
  formatElapsed,
  latestBlocksByLabel,
  parseUtcIsoMs,
  toolCallIdOf,
} from "./narrativeState";

export { AUTHORING_TOOLS, RUN_TOOLS };

export type CopilotPhaseId = "explore" | "draft" | "test" | "done";
export type PhaseStatus =
  | "pending"
  | "active"
  | "done"
  | "fail"
  | "stopped"
  | "notrun";

export interface PhaseRowModel {
  id: CopilotPhaseId;
  label: string;
  status: PhaseStatus;
  stub: string | null;
  // Hosted stream for explore/draft; test-tool rows (not block cards) for test.
  entries: ActivityEntry[];
}

const PHASE_LABELS: Record<CopilotPhaseId, string> = {
  explore: "Explore site",
  draft: "Draft code",
  test: "Test-run",
  done: "Done",
};

// Byte-for-byte the existing showDesign gate (NarrativeView.tsx) so Q&A /
// clarify turns keep today's behavior: checklist appears live, disappears at
// a no-build terminal.
export function showPhaseChecklist(turn: TurnNarrativeState): boolean {
  return (
    turn.designStarted &&
    ((turn.draft?.blockCount ?? 0) > 0 ||
      turn.blocks.length > 0 ||
      turn.terminal === null)
  );
}

// A tool_call still has no matching tool_result — the narrator can emit a
// TOOL_STARTED progress narration mid-flight (streaming_adapter.py), so
// checking only the LAST entry's kind isn't enough; match ids instead.
function hasPendingToolCall(designActivity: ActivityEntry[]): boolean {
  const pending = new Set<string>();
  for (const entry of designActivity) {
    if (entry.kind === "tool_call") {
      pending.add(toolCallIdOf(entry) ?? "");
    } else if (entry.kind === "tool_result") {
      pending.delete(toolCallIdOf(entry) ?? "");
    }
  }
  return pending.size > 0;
}

// Whether the 8s drafting-gap timer should arm for this narrative snapshot.
// A tool call with no matching result yet is still executing (e.g. a slow
// navigate_browser) — that's "tool executing", not "LLM silently drafting",
// so a pending round-trip must not arm the timer.
export function shouldArmDraftingGapTimer(turn: TurnNarrativeState): boolean {
  if (
    turn.turnId === null ||
    turn.terminal !== null ||
    turn.draftingSignaledAt !== null
  ) {
    return false;
  }
  if (
    turn.draft !== null ||
    turn.designEnded ||
    turn.blocks.some((b) => b.state !== "drafted")
  ) {
    return false;
  }
  if (!turn.designActivity.some((e) => e.kind === "tool_call")) return false;
  if (hasPendingToolCall(turn.designActivity)) return false;
  return turn.lastActivityAtMs !== null;
}

function bucketActivity(designActivity: ActivityEntry[]): {
  explore: ActivityEntry[];
  draft: ActivityEntry[];
  test: ActivityEntry[];
} {
  const explore: ActivityEntry[] = [];
  const draft: ActivityEntry[] = [];
  const test: ActivityEntry[] = [];
  let authoringSeen = false;
  for (const entry of designActivity) {
    // Set authoringSeen from AUTHORING_TOOLS independent of the RUN_TOOLS
    // bucket check below — update_workflow is authoring but not a run tool,
    // so it must still flip the explore→draft boundary even though its own
    // entry falls through to the draft push, not the test push.
    if (entry.toolName && AUTHORING_TOOLS.has(entry.toolName)) {
      authoringSeen = true;
    }
    if (entry.toolName && RUN_TOOLS.has(entry.toolName)) {
      test.push(entry);
      continue;
    }
    (authoringSeen ? draft : explore).push(entry);
  }
  return { explore, draft, test };
}

function lastAuthoringToolName(
  designActivity: ActivityEntry[],
): string | undefined {
  for (let i = designActivity.length - 1; i >= 0; i--) {
    const entry = designActivity[i]!;
    if (
      entry.kind === "tool_call" &&
      entry.toolName &&
      AUTHORING_TOOLS.has(entry.toolName)
    ) {
      return entry.toolName;
    }
  }
  return undefined;
}

function countAuthoringToolCalls(designActivity: ActivityEntry[]): number {
  let count = 0;
  for (const entry of designActivity) {
    if (
      entry.kind === "tool_call" &&
      entry.toolName &&
      AUTHORING_TOOLS.has(entry.toolName)
    ) {
      count += 1;
    }
  }
  return count;
}

function pluralize(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
}

// Earliest startedAt / latest endedAt across a block set, as ISO strings so
// the existing formatElapsed can render them — mixing client/server clocks
// per-block would lie, so this only ever combines same-source timestamps.
function spanIso(blocks: BlockState[]): {
  start: string | null;
  end: string | null;
} {
  let startMs: number | null = null;
  let start: string | null = null;
  let endMs: number | null = null;
  let end: string | null = null;
  for (const b of blocks) {
    const sMs = parseUtcIsoMs(b.startedAt);
    if (sMs !== null && (startMs === null || sMs < startMs)) {
      startMs = sMs;
      start = b.startedAt;
    }
    const eMs = parseUtcIsoMs(b.endedAt);
    if (eMs !== null && (endMs === null || eMs > endMs)) {
      endMs = eMs;
      end = b.endedAt;
    }
  }
  return { start, end };
}

export function derivePhases(turn: TurnNarrativeState): PhaseRowModel[] {
  const {
    explore,
    draft: draftEntries,
    test,
  } = bucketActivity(turn.designActivity);
  const lastAuthoring = lastAuthoringToolName(turn.designActivity);
  const latestBlocks = latestBlocksByLabel(turn.blocks).filter(
    (b) => b.state !== "drafted",
  );
  const testReached =
    latestBlocks.length > 0 ||
    (turn.designEnded && lastAuthoring === "update_and_run_blocks");
  const draftReached =
    turn.authoringCount > 0 ||
    turn.draft !== null ||
    turn.designEnded ||
    turn.draftingSignaledAt !== null ||
    testReached;

  const running = turn.blocks.some(
    (b) => b.state === "running" || b.state === "queued",
  );
  const lastRunOutcome = turn.lastRunOutcome;
  // The loop demonstrably continued past the failed verdict (a new activity
  // frame arrived), distinguishing "revising" from "composing the give-up
  // terminal response". Compares the monotonic activitySeq, not
  // designActivity.length — the latter plateaus once MAX_DESIGN_ACTIVITY_ENTRIES
  // is reached, which would otherwise make this comparison never fire again.
  const redrafting =
    turn.terminal === null &&
    !running &&
    lastRunOutcome !== null &&
    (lastRunOutcome.verdict === "not_demonstrated" ||
      lastRunOutcome.verdict === "not_evaluated") &&
    turn.activitySeq > lastRunOutcome.activitySeqAtVerdict;

  const chainActive: CopilotPhaseId = testReached
    ? "test"
    : draftReached
      ? "draft"
      : "explore";
  const liveActive: CopilotPhaseId | null =
    turn.terminal !== null
      ? null
      : running || lastRunOutcome?.verdict === "evaluating"
        ? "test"
        : redrafting
          ? "draft"
          : chainActive;

  const isTerminal = turn.terminal !== null;
  const isError = turn.terminal === "error";
  const isCancelled = turn.cancelled === true;
  const anyFailed = latestBlocks.some((b) => b.state === "failed");
  const anyNotDemonstrated = latestBlocks.some(
    (b) => b.outcome === "not_demonstrated",
  );

  function reached(id: CopilotPhaseId): boolean {
    if (id === "explore") return true;
    if (id === "draft") return draftReached;
    return testReached;
  }

  function statusFor(id: CopilotPhaseId): PhaseStatus {
    if (id === "done") {
      if (!isTerminal) return "pending";
      if (turn.terminal === "response" && !isCancelled) return "done";
      return isError ? "fail" : "stopped";
    }
    // A hard block failure wins regardless of live/terminal — unless this
    // phase is still the live-active row (a block is still running/queued).
    if (id === "test" && testReached && anyFailed && id !== liveActive) {
      return "fail";
    }
    if (!isTerminal) {
      return id === liveActive ? "active" : reached(id) ? "done" : "pending";
    }
    if (!reached(id)) return id === "test" ? "notrun" : "pending";
    if (chainActive === id) {
      if (isError) return "fail";
      if (isCancelled) return "stopped";
    }
    return "done";
  }

  // Condensed for display only — stubFor/redrafting/activitySeq above all
  // read the raw explore/draftEntries/test closures, not this map.
  const entriesFor: Record<CopilotPhaseId, ActivityEntry[]> = {
    explore: condenseActivityEntries(explore),
    draft: condenseActivityEntries(draftEntries),
    test: condenseActivityEntries(test),
    done: [],
  };

  function stubFor(id: CopilotPhaseId, status: PhaseStatus): string | null {
    if (id === "done") return null;
    if (id === "explore") {
      if (status !== "done" && status !== "fail" && status !== "stopped") {
        return null;
      }
      // Condensed count, not raw: a folded retry now renders as one row,
      // so the stub must match what expanding the row actually shows.
      const n = entriesFor.explore.filter(
        (e) => e.toolName !== undefined,
      ).length;
      return n > 0 ? pluralize(n, "step") : null;
    }
    if (id === "draft") {
      if (status !== "done" && status !== "fail" && status !== "stopped") {
        return null;
      }
      if (!turn.draft) return null;
      const base = pluralize(turn.draft.blockCount, "block");
      if (!isTerminal) return base;
      // Intentionally a raw authoring-attempt count, not a rendered-row
      // count: it spans update_workflow (draft bucket) AND
      // update_and_run_blocks (test bucket), so there's no single
      // condensed bucket to count against the way the explore stub does.
      const drafts = countAuthoringToolCalls(turn.designActivity);
      return drafts >= 2 ? `${base} · ${drafts} drafts` : base;
    }
    // test
    if (status === "notrun") return "not run";
    if (status === "pending" || status === "active") return null;
    if (status === "fail")
      return `${pluralize(latestBlocks.length, "block")} · failed`;
    if (status === "stopped") {
      return latestBlocks.length > 0
        ? `${pluralize(latestBlocks.length, "block")} · stopped`
        : "stopped";
    }
    if (anyNotDemonstrated) return "· not confirmed";
    const { start, end } = spanIso(latestBlocks);
    const elapsed = formatElapsed(start, end);
    return elapsed
      ? `${pluralize(latestBlocks.length, "block")} · ${elapsed}`
      : pluralize(latestBlocks.length, "block");
  }

  return (["explore", "draft", "test", "done"] as const).map((id) => {
    const status = statusFor(id);
    return {
      id,
      label: PHASE_LABELS[id],
      status,
      stub: stubFor(id, status),
      entries: entriesFor[id],
    };
  });
}
