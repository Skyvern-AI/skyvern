import { CodeWriteDiff } from "./workflowCopilotTypes";
import {
  ActivityEntry,
  AUTHORING_TOOLS,
  BlockState,
  RUN_TOOLS,
  TurnNarrativeState,
  condenseActivityEntries,
  hasPendingToolCall,
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
    const first = row.entries[0];
    if (first === undefined) return null;
    const callId = toolCallIdOf(first);
    const startedAt =
      (callId === undefined ? undefined : callStartedAt.get(callId)) ??
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
  const lastRow = runRows[runRows.length - 1];
  const blockStartedMs = parseUtcIsoMs(block.startedAt);
  if (blockStartedMs === null) {
    return lastRow;
  }
  let anchor: ActivityRow | undefined;
  for (const row of runRows) {
    const startedMs = runStartedMs(row);
    if (startedMs !== null && startedMs <= blockStartedMs) {
      anchor = row;
    }
  }
  // Nothing started early enough means the block's own row aged out past the
  // activity cap, so the nearest surviving row is the earliest one left.
  return anchor ?? runRows[0];
}

function condensedBlock(block: BlockState): BlockState {
  return { ...block, activity: condenseActivityEntries(block.activity) };
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
    if (kind === "browse" && prev?.kind === "browse") {
      prev.entries.push(entry);
      continue;
    }
    rows.push({
      id: toolCallIdOf(entry) || entry.id,
      kind,
      entries: [entry],
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

  // Pair by iteration when the tag survived, else attach to the step the
  // narration followed: iteration is neither unique nor durable.
  for (const { entry, precedingRow } of narrations) {
    // The owning row can sit either side of where the narration landed: a
    // narration emitted mid-step precedes its own tool_result, while one
    // emitted after a step follows it. Iteration also restarts each
    // enforcement pass, so nearest-wins disambiguates a repeated number.
    let ownerIdx = -1;
    let bestDistance = Infinity;
    rows.forEach((row, i) => {
      if (!row.entries.some((e) => e.iteration === entry.iteration)) return;
      const distance = Math.abs(i - precedingRow);
      if (distance < bestDistance) {
        bestDistance = distance;
        ownerIdx = i;
      }
    });
    const targetIdx = ownerIdx === -1 ? precedingRow : ownerIdx;
    const target = rows[targetIdx];
    if (target) {
      target.reason = entry.text;
      // The reason tracks the latest narration, but the live title does not:
      // the first narration to reach a row names the work, and a later one
      // would rewrite a line the user is already reading. The outcome title
      // still comes from the latest narration, which is the one that knows how
      // the row ended.
      const held = labelsByRow.get(targetIdx);
      labelsByRow.set(targetIdx, {
        active: held?.active ?? entry.activeLabel,
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
      .map((e) => e.timestamp)
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
    row.label =
      (working ? tenses.active : (tenses.outcome ?? tenses.active)) ?? null;
  });

  // While the model is generating, no row has an unmatched call and no block is
  // running, so liveIndex is -1 and nothing would be open — the stretch that
  // reads as "one collapsed row and nothing going on". Rows only ever append,
  // so following the newest one keeps the freshest work open and moves forward
  // only, instead of hopping back to an earlier row when a parallel call
  // finishes and collapsing the row being read.
  const focusIndex = ended || rows.length === 0 ? -1 : rows.length - 1;

  return { rows, liveIndex, focusIndex };
}
