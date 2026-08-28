import { CodeWriteDiff } from "./workflowCopilotTypes";
import {
  ActivityEntry,
  AUTHORING_TOOLS,
  BlockState,
  RUN_TOOLS,
  TurnNarrativeState,
  condenseActivityEntries,
  hasPendingToolCall,
  isBlockOk,
  parseUtcIsoMs,
  toolCallIdOf,
} from "./narrativeState";

export type ActivityKind = "browse" | "author" | "run";

export const ACTIVITY_KIND_GLYPH: Record<ActivityKind, string> = {
  browse: "◎",
  author: "⟨⟩",
  run: "▷",
};

// The glyph is aria-hidden, so the kind reaches screen readers as this word
// instead — not every row's own text implies which kind it is.
export const ACTIVITY_KIND_WORD: Record<ActivityKind, string> = {
  browse: "Looked at the page",
  author: "Wrote code",
  run: "Ran it",
};

export interface ActivityRow {
  id: string;
  kind: ActivityKind | null;
  entries: ActivityEntry[];
  blocks: BlockState[];
  // Line deltas the writes in this row produced, newest entry winning per block
  // label. Empty for every row the backend sent no diff for.
  codeDiffs: CodeWriteDiff[];
  // Some call in this row has no matching result yet. A row's calls can
  // resolve out of order, so its last entry alone does not answer this.
  pending: boolean;
  // The work this row describes is still happening: a call without its result,
  // or a block still running, on a turn that has not ended. Drives the ticking
  // clock, so it must go false at turn end even if a call never resolved.
  live: boolean;
  // Narrator prose explaining why this step happened. Latest narration wins
  // when a merged row spans several iterations.
  reason: string | null;
  // Narrator-authored title for this step, in the tense matching its state.
  // Null when the narrator never spoke for it, leaving the tool-derived label
  // as the row's title.
  label: string | null;
  // First and last server clock reads across this row's entries, so a merged
  // row reports the span of the work rather than one entry's instant.
  startedAt: string | null;
  endedAt: string | null;
  // Epoch ms the winning narration arrived live; absent on hydrate.
  reasonAt?: number;
}

export interface ActivityLog {
  rows: ActivityRow[];
  // The one row still working, or -1. The scan keeps the last qualifying row,
  // so parallel tool calls and a block left `running` cannot both claim it.
  liveIndex: number;
  // Row the reader should be looking at. Distinct from liveIndex: liveness is
  // strict (an unmatched call or a running block) and drives tense, while focus
  // has to survive the gap between one call returning and the next being made.
  focusIndex: number;
}

// Block-scoped authoring tools, absent from narrativeState's AUTHORING_TOOLS.
// The rail buckets them as pre-authoring today (SKY-14524); this set diverges
// deliberately rather than widening AUTHORING_TOOLS and moving the rail.
const LOG_AUTHORING_TOOLS = new Set([
  "edit_block",
  "add_block",
  "delete_block",
]);

// update_and_run_blocks belongs to both sets, so RUN_TOOLS has to win — the
// same precedence bucketActivity already applies.
export function kindOf(entry: ActivityEntry): ActivityKind | null {
  const toolName = entry.toolName;
  if (entry.kind === "narration" || toolName === undefined) {
    return null;
  }
  if (RUN_TOOLS.has(toolName)) {
    return "run";
  }
  if (AUTHORING_TOOLS.has(toolName) || LOG_AUTHORING_TOOLS.has(toolName)) {
    return "author";
  }
  return "browse";
}

// Condensing keeps the tool_result, so a finished row's own entry is stamped
// when the run ENDED — and a block always starts before its run reports back.
// The invocation time survives on the paired tool_call in the uncondensed
// activity, which is what orders the rows.
function runStartLookup(
  designActivity: ActivityEntry[],
): (row: ActivityRow) => number | null {
  const callStartedAt = new Map<string, string>();
  for (const entry of designActivity) {
    if (entry.kind !== "tool_call" || entry.timestamp === undefined) continue;
    const callId = toolCallIdOf(entry);
    if (callId !== undefined && !callStartedAt.has(callId)) {
      callStartedAt.set(callId, entry.timestamp);
    }
  }
  return (row) => {
    // A code write and its immediate test share one display row, but block
    // evidence still belongs to the run invocation. Using the author entry's
    // earlier timestamp here can make a parallel block look like part of the
    // new test.
    const firstRun = row.entries.find((entry) => kindOf(entry) === "run");
    const first = firstRun ?? row.entries[0];
    if (first === undefined) return null;
    const callId = toolCallIdOf(first);
    const startedAt =
      (callId === undefined ? undefined : callStartedAt.get(callId)) ??
      first.activityStartedAt ??
      first.timestamp;
    return parseUtcIsoMs(startedAt);
  };
}

// `iteration` restarts at 0 on every enforcement pass while the rows keep
// accumulating, so it cannot order rows across a turn. The server clock can:
// the producer is the last run row that had already started when the block did.
function anchorRunRow(
  block: BlockState,
  runRows: ActivityRow[],
  runStartedMs: (row: ActivityRow) => number | null,
): ActivityRow | undefined {
  if (block.state === "drafted" || runRows.length === 0) {
    return undefined;
  }
  const timedRows = runRows.map((row) => ({ row, started: runStartedMs(row) }));
  const blockStartedMs = parseUtcIsoMs(block.startedAt);
  if (blockStartedMs === null) {
    return (
      timedRows.reduce<ActivityRow | undefined>((latest, candidate) => {
        if (candidate.started === null) return latest;
        if (latest === undefined) return candidate.row;
        const latestStarted = runStartedMs(latest);
        return latestStarted === null || candidate.started > latestStarted
          ? candidate.row
          : latest;
      }, undefined) ?? runRows[runRows.length - 1]
    );
  }
  let anchor: ActivityRow | undefined;
  let anchorStarted = -Infinity;
  for (const { row, started } of timedRows) {
    if (
      started !== null &&
      started <= blockStartedMs &&
      started > anchorStarted
    ) {
      anchor = row;
      anchorStarted = started;
    }
  }
  // Nothing started early enough means the block's own row aged out past the
  // activity cap, so the nearest surviving row is the earliest one left.
  if (anchor !== undefined) return anchor;
  return (
    timedRows.reduce<ActivityRow | undefined>((earliest, candidate) => {
      if (candidate.started === null) return earliest;
      if (earliest === undefined) return candidate.row;
      const earliestStarted = runStartedMs(earliest);
      return earliestStarted === null || candidate.started < earliestStarted
        ? candidate.row
        : earliest;
    }, undefined) ?? runRows[0]
  );
}

function retryEntries(entry: ActivityEntry): ActivityEntry[] {
  return [...(entry.priorFailures ?? []), entry];
}

function condensedBlock(block: BlockState): BlockState {
  return {
    ...block,
    activity: condenseActivityEntries(block.activity).flatMap(retryEntries),
  };
}

function stableRowId(entry: ActivityEntry): string {
  const root = entry.retryRootId;
  if (root !== undefined) {
    return root.startsWith("tc-") || root.startsWith("tr-")
      ? root.slice(3)
      : root;
  }
  return toolCallIdOf(entry) || entry.id;
}

function coalesceNarratedBrowseRetries(
  rows: ActivityRow[],
  intentByRow: ReadonlyMap<ActivityRow, string>,
): void {
  const coalesced: ActivityRow[] = [];
  const coalescedIntents: (string | undefined)[] = [];
  for (const row of rows) {
    const previous = coalesced[coalesced.length - 1];
    const intent = intentByRow.get(row);
    const previousIntent = coalescedIntents[coalescedIntents.length - 1];
    const previousTail = previous?.entries[previous.entries.length - 1];
    const previousFailed =
      previousTail?.kind === "tool_result" && previousTail.success === false;
    const previousEndedMs = parseUtcIsoMs(previous?.endedAt);
    const rowStartedMs = parseUtcIsoMs(row.startedAt);
    // Parallel siblings can carry the same narrated intent. They are a retry
    // only when the new attempt starts after the failed one ended; missing
    // timestamps retain the legacy fallback for old hydrated payloads.
    const followsFailure =
      previousEndedMs === null ||
      rowStartedMs === null ||
      rowStartedMs >= previousEndedMs;
    if (
      previous?.kind !== "browse" ||
      row.kind !== "browse" ||
      !previousFailed ||
      !followsFailure ||
      intent === undefined ||
      intent !== previousIntent
    ) {
      coalesced.push(row);
      coalescedIntents.push(intent);
      continue;
    }

    const latest = row.entries[row.entries.length - 1];
    if (latest === undefined) {
      coalesced.push(row);
      coalescedIntents.push(intent);
      continue;
    }
    const attempts =
      (previous.entries[previous.entries.length - 1]?.attempts ?? 1) +
      (latest.attempts ?? 1);
    const earlierEntries = previous.entries.map((entry) => ({
      ...entry,
      attempts: undefined,
    }));
    const currentEntries = row.entries.slice();
    currentEntries[currentEntries.length - 1] = {
      ...latest,
      attempts,
      activityStartedAt:
        previous.startedAt ?? latest.activityStartedAt ?? latest.timestamp,
    };
    coalesced[coalesced.length - 1] = {
      ...row,
      id: previous.id,
      entries: [...earlierEntries, ...currentEntries],
      startedAt: previous.startedAt ?? row.startedAt,
      reason: row.reason ?? previous.reason,
      reasonAt: row.reasonAt ?? previous.reasonAt,
    };
  }
  rows.splice(0, rows.length, ...coalesced);
}

function rowContainsNarrationTime(
  row: ActivityRow,
  narrationTimestamp: string | undefined,
): boolean {
  const narrationMs = parseUtcIsoMs(narrationTimestamp);
  if (narrationMs === null || row.entries.length === 0) return false;

  const starts = row.entries
    .flatMap((entry) => [entry.activityStartedAt, entry.timestamp])
    .map(parseUtcIsoMs)
    .filter((value): value is number => value !== null);
  if (starts.length === 0) return false;

  const startedMs = Math.min(...starts);
  const endedMs = hasPendingToolCall(row.entries)
    ? Infinity
    : Math.max(...starts);
  return narrationMs >= startedMs && narrationMs <= endedMs;
}

export function deriveActivityLog(turn: TurnNarrativeState): ActivityLog {
  const rows: ActivityRow[] = [];
  const narrations: { entry: ActivityEntry; precedingRow: number }[] = [];
  // Both tenses are collected first; which one reads depends on liveIndex,
  // which is only known once every row exists.
  const labelsByRow = new Map<number, { active?: string; outcome?: string }>();
  for (const entry of condenseActivityEntries(turn.designActivity)) {
    if (entry.kind === "narration") {
      narrations.push({ entry, precedingRow: rows.length - 1 });
      continue;
    }
    const kind = kindOf(entry);
    const prev = rows[rows.length - 1];
    const previousEntry = prev?.entries[prev.entries.length - 1];
    const previousEndedMs = parseUtcIsoMs(previousEntry?.timestamp);
    const runStartedMs = parseUtcIsoMs(
      entry.activityStartedAt ?? entry.timestamp,
    );
    const followsPreviousEntry =
      previousEndedMs === null ||
      runStartedMs === null ||
      runStartedMs >= previousEndedMs;
    // Successful and in-flight browse tools form one compact discovery row.
    // A failed browse tool is its own recoverable attempt: folding it into the
    // surrounding successes made an earlier successful action appear to fail,
    // while folding multiple failures reused the wrong action title. Same-tool
    // retries have already been condensed above and retain their attempt count.
    if (
      kind === "browse" &&
      prev?.kind === "browse" &&
      entry.success !== false &&
      previousEntry?.success !== false
    ) {
      prev.entries.push(...retryEntries(entry));
      continue;
    }
    // A block-scoped write and the run immediately following it are one user-
    // facing build/test action. Keeping them on one frontier preserves the
    // freshly written diff while the test is active instead of flashing the
    // write row for a moment and collapsing it as soon as the run starts.
    if (
      kind === "run" &&
      prev?.kind === "author" &&
      followsPreviousEntry &&
      prev.entries.some((candidate) => (candidate.codeDiffs?.length ?? 0) > 0)
    ) {
      prev.kind = "run";
      prev.entries.push(...retryEntries(entry));
      continue;
    }
    rows.push({
      id: stableRowId(entry),
      kind,
      entries: retryEntries(entry),
      blocks: [],
      codeDiffs: [],
      pending: false,
      live: false,
      reason: null,
      label: null,
      startedAt: null,
      endedAt: null,
    });
  }

  // Pair a narration to a result that chronology moved below it, then by the
  // closest matching iteration, else to the step it followed. A pending row's
  // open-ended time span is not an ownership claim: parallel calls commonly
  // overlap, and an older unresolved call must not steal a sibling's prose.
  for (const { entry, precedingRow } of narrations) {
    // The owning row can sit either side of where the narration landed: a
    // narration emitted mid-step precedes its own tool_result, while one
    // emitted after a step follows it. Iteration also restarts each
    // enforcement pass, so nearest-wins disambiguates a repeated number.
    // Pairing a call/result whose narration arrived mid-flight moves its result
    // after the narration. Only a later row can be that displaced owner. This
    // narrow timestamp rule keeps "Reviewing…" with the inspection it describes
    // without letting any older open-ended call capture unrelated narration.
    let ownerIdx = -1;
    const futureTimeOwners: number[] = [];
    for (let i = precedingRow + 1; i < rows.length; i += 1) {
      if (rowContainsNarrationTime(rows[i]!, entry.timestamp)) {
        futureTimeOwners.push(i);
      }
    }
    if (futureTimeOwners.length === 1) {
      ownerIdx = futureTimeOwners[0]!;
    } else if (futureTimeOwners.length > 1) {
      const matchingIteration = futureTimeOwners.filter((i) =>
        rows[i]!.entries.some(
          (candidate) => candidate.iteration === entry.iteration,
        ),
      );
      if (matchingIteration.length === 1) {
        ownerIdx = matchingIteration[0]!;
      }
    }
    let bestDistance = Infinity;
    if (ownerIdx === -1) {
      rows.forEach((row, i) => {
        if (!row.entries.some((e) => e.iteration === entry.iteration)) return;
        const distance = Math.abs(i - precedingRow);
        if (distance < bestDistance) {
          bestDistance = distance;
          ownerIdx = i;
        }
      });
    }
    const targetIdx = ownerIdx === -1 ? precedingRow : ownerIdx;
    const target = rows[targetIdx];
    if (target) {
      target.reason = entry.text;
      // The reason tracks the latest narration, but the live title normally
      // does not: the first narration to reach a row names the work, and a
      // later one would rewrite a line the user is already reading. A combined
      // write/test row is the exception because its current action genuinely
      // advances from writing to testing while preserving one frontier.
      const held = labelsByRow.get(targetIdx);
      const combinedWriteAndRun =
        target.kind === "run" &&
        target.entries.some((candidate) => kindOf(candidate) === "author");
      labelsByRow.set(targetIdx, {
        active: combinedWriteAndRun
          ? (entry.activeLabel ?? held?.active)
          : (held?.active ?? entry.activeLabel),
        outcome: entry.outcomeLabel ?? held?.outcome,
      });
      target.reasonAt = entry.receivedAtMs;
    }
  }

  // Block rows are appended below, so the indices held in labelsByRow stay valid.
  const runRows = rows.filter((r) => r.kind === "run");
  const runStartedMs = runStartLookup(turn.designActivity);
  for (const block of turn.blocks) {
    const anchor = anchorRunRow(block, runRows, runStartedMs);
    if (anchor) {
      anchor.blocks.push(condensedBlock(block));
      continue;
    }
    // Drafted blocks, and any block in a turn that never ran, get a row of
    // their own so folding the log never strands a card outside one.
    rows.push({
      id: `block-${block.workflowRunBlockId || block.label}`,
      kind: block.state === "drafted" ? "author" : "run",
      entries: [],
      blocks: [condensedBlock(block)],
      codeDiffs: [],
      pending: false,
      live: false,
      reason: null,
      label: null,
      startedAt: null,
      endedAt: null,
    });
  }

  // A cancelled or timed-out turn can terminate with a call still unmatched;
  // nothing is working once the turn is over, so nothing claims the open row.
  const ended = turn.terminal !== null;
  let liveIndex = -1;
  rows.forEach((row) => {
    const stamps = row.entries
      .flatMap((e) => [e.activityStartedAt, e.timestamp])
      .filter((t): t is string => typeof t === "string")
      .sort();
    row.startedAt = stamps[0] ?? null;
    row.endedAt = stamps[stamps.length - 1] ?? null;
  });

  rows.forEach((row, i) => {
    const byLabel = new Map<string, CodeWriteDiff>();
    for (const entry of row.entries) {
      for (const diff of entry.codeDiffs ?? []) byLabel.set(diff.label, diff);
    }
    // A repair tool_call is bucketed under the block that is already running,
    // not in designActivity. Its write-time diff still belongs to the block's
    // frontier row and must be promoted before the later tool_result repeats it.
    for (const block of row.blocks) {
      for (const entry of block.activity) {
        for (const diff of entry.codeDiffs ?? []) byLabel.set(diff.label, diff);
      }
    }
    row.codeDiffs = [...byLabel.values()];
    row.pending = hasPendingToolCall(row.entries);
    row.live =
      !ended && (row.pending || row.blocks.some((b) => b.state === "running"));
    if (row.live) {
      liveIndex = i;
    }
  });

  rows.forEach((row, i) => {
    const tenses = labelsByRow.get(i);
    if (!tenses) return;
    // A row still carrying an unmatched call has not finished, whatever
    // liveIndex says: it is -1 on a terminated turn, and last-wins when two
    // calls run in parallel. A step that never returned cannot show an outcome.
    const working = row.pending || i === liveIndex;
    // Outcome labels are predictions authored before the step resolves. When
    // the attempt fails, keep the narrator's stable intent as the row title;
    // the exact failure remains in the row detail instead of letting a stale
    // success-shaped outcome replace what Copilot was trying to accomplish.
    const terminalEntry = row.entries[row.entries.length - 1];
    const cannotUsePredictedOutcome =
      (terminalEntry?.kind === "tool_result" &&
        terminalEntry.success === false) ||
      row.blocks.some((block) => !isBlockOk(block));
    row.label =
      (working || cannotUsePredictedOutcome
        ? tenses.active
        : (tenses.outcome ?? tenses.active)) ?? null;
  });

  // Narration names the user-facing activity; raw browser operations are its
  // technical substeps. Until the narrator declares a new intent, keep those
  // substeps under the current active label instead of flashing peer titles
  // such as "Inspecting page" and "Opening page" between semantic labels.
  let browseIntent: string | null = null;
  const browseIntentByRow = new Map<ActivityRow, string>();
  rows.forEach((row, i) => {
    if (row.kind !== "browse") {
      browseIntent = null;
      return;
    }
    const active = labelsByRow.get(i)?.active;
    if (active !== undefined) {
      browseIntent = active;
    } else if (browseIntent !== null) {
      row.label = browseIntent;
    }
    if (browseIntent !== null) {
      browseIntentByRow.set(row, browseIntent);
    }
  });

  // Retries may switch browser tools while pursuing the same narrated intent.
  // Tool-level condensation cannot recognize that as one activity, but the
  // narrator's explicit active label can: adjacent failed attempts with the
  // same label become one row. The first attempt keeps the stable row id so a
  // user's expansion choice survives while later attempts update its status.
  coalesceNarratedBrowseRetries(rows, browseIntentByRow);
  liveIndex = -1;
  rows.forEach((row, i) => {
    row.pending = hasPendingToolCall(row.entries);
    row.live =
      !ended &&
      (row.pending || row.blocks.some((block) => block.state === "running"));
    if (row.live) liveIndex = i;
  });

  // While the model is generating, no row has an unmatched call and no block is
  // running, so liveIndex is -1 and nothing would be open — the stretch that
  // reads as "one collapsed row and nothing going on". Follow the newest row in
  // that gap. The one exception is a contentless draft placeholder appended
  // after real live work: it describes what now exists, not what is happening,
  // so it cannot hide the row carrying the active call or running block.
  const newestIndex = rows.length - 1;
  const newest = rows[newestIndex];
  const newestIsEmptyDraft =
    newest !== undefined &&
    newest.entries.length === 0 &&
    newest.codeDiffs.length === 0 &&
    newest.reason === null &&
    newest.blocks.length > 0 &&
    newest.blocks.every(
      (block) =>
        block.state === "drafted" &&
        block.activity.length === 0 &&
        (block.recordedActions?.length ?? 0) === 0,
    );
  const focusIndex =
    ended || rows.length === 0
      ? -1
      : liveIndex !== -1 && newestIsEmptyDraft
        ? liveIndex
        : newestIndex;

  return { rows, liveIndex, focusIndex };
}
