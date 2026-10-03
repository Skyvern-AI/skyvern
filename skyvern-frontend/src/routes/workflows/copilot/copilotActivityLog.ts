import { CodeWriteDiff } from "./workflowCopilotTypes";
import {
  ActivityEntry,
  AUTHORING_TOOLS,
  BlockState,
  RUN_TOOLS,
  ToolCallKind,
  TurnFacts,
  TurnNarrativeState,
  blockPassed,
  condenseActivityEntries,
  hasObservedBlockEvidence,
  hasPendingToolCall,
  parseUtcIsoMs,
  ranCleanOnCurrentSource,
  toolActivityDisplayLabel,
  toolCallIdOf,
  toolCallKind,
} from "./narrativeState";

export type ActivityKind = "browse" | "author" | "run";

// Not every row's own text implies which kind it is, so screen readers hear
// the kind as this word.
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
  // or a block still running, on a turn that has not ended. It goes false at
  // turn end even if a call never resolved.
  live: boolean;
  // Actor metadata belongs to the call; legacy unowned text has its own row.
  reason: string | null;
  // First and last server clock reads across this row's entries. A browse
  // retry merges only when it started after the failed attempt ended.
  startedAt: string | null;
  endedAt: string | null;
  // Epoch ms the winning narration arrived live; absent on hydrate.
  reasonAt?: number;
  // Block labels the model has written so far in the authoring call it is
  // still streaming. Present only on the synthetic live drafting row.
  draftingLabels?: string[];
}

export interface ActivityLog {
  rows: ActivityRow[];
  // The one row showing a loader, or -1: the last row with an unmatched call or
  // a running block, else the newest step while the model works between calls.
  liveIndex: number;
  // Row the reader should be looking at. Distinct from liveIndex: focus
  // survives the end of the turn's work, and a trailing sentence waiting for
  // its first call is not where the reader looks.
  focusIndex: number;
}

// Block-scoped authoring tools, absent from narrativeState's AUTHORING_TOOLS.
const LOG_AUTHORING_TOOLS = new Set([
  "edit_block",
  "add_block",
  "delete_block",
]);

// update_and_run_blocks belongs to both sets, so RUN_TOOLS has to win.
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

function reasonOnlyRow(narration: ActivityEntry): ActivityRow {
  return {
    // The same recorded UTC timestamp may arrive with Z or +00:00.
    // Normalize only the machine-generated identity, retaining subsecond precision.
    id: `reason-${narration.id.replace("+00:00", "Z")}`,
    kind: null,
    entries: [],
    blocks: [],
    codeDiffs: [],
    pending: false,
    live: false,
    reason: narration.text,
    reasonAt: narration.receivedAtMs,
    startedAt: null,
    endedAt: null,
  };
}

// The newest step with calls, unless a narration has opened a line after it.
function newestStepIndex(rows: ActivityRow[]): number {
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    if (isReasonOnlyRow(rows[i]!)) return -1;
    if (rows[i]!.entries.length > 0) return i;
  }
  return -1;
}

export function isReasonOnlyRow(row: ActivityRow): boolean {
  return (
    row.entries.length === 0 && row.blocks.length === 0 && row.reason !== null
  );
}

// The row holding a tool call's activity, as a row entry or on a block the row ran. -1 when the call
// has no row: it aged out past the activity cap, or its frames never reached this turn.
export function rowIndexOfToolCall(
  rows: ActivityRow[],
  toolCallId: string,
): number {
  const ofCall = (entry: ActivityEntry) => toolCallIdOf(entry) === toolCallId;
  return rows.findIndex(
    (row) =>
      row.entries.some(ofCall) ||
      row.blocks.some((block) => block.activity.some(ofCall)),
  );
}

// A call whose card renders after its row ends that row, so work done after the
// user answered or the plan changed never sits above the card.
const CARD_TOOLS = new Set(["ask_user", "set_work_plan"]);

export function deriveActivityLog(turn: TurnNarrativeState): ActivityLog {
  // A cancelled or timed-out turn can terminate with a call still unmatched;
  // nothing is working once the turn is over, so nothing claims the open row.
  const ended = turn.terminal !== null;
  const rows: ActivityRow[] = [];
  for (const entry of condenseActivityEntries(turn.designActivity)) {
    const last = rows[rows.length - 1];
    if (entry.kind === "narration") {
      rows.push(reasonOnlyRow(entry));
      continue;
    }
    const kind = kindOf(entry);
    const prev = last;
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
      entry.reason === undefined &&
      prev?.reason === null &&
      kind === "browse" &&
      prev?.kind === "browse" &&
      entry.success !== false &&
      previousEntry?.success !== false &&
      !CARD_TOOLS.has(previousEntry?.toolName ?? "")
    ) {
      prev.entries.push(...retryEntries(entry));
      continue;
    }
    // A block-scoped write and the run immediately following it are one user-
    // facing build/test action. Keeping them on one frontier preserves the
    // freshly written diff while the test is active instead of flashing the
    // write row for a moment and collapsing it as soon as the run starts.
    if (
      entry.reason === undefined &&
      prev?.reason === null &&
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
      reason: entry.reason ?? null,
      reasonAt: entry.receivedAtMs,
      startedAt: null,
      endedAt: null,
    });
  }

  const runRows = rows.filter((r) => r.kind === "run");
  const runStartedMs = runStartLookup(turn.designActivity);
  for (const block of turn.blocks.filter(hasObservedBlockEvidence)) {
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
      startedAt: null,
      endedAt: null,
    });
  }

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

  // The model is still writing the authoring call's arguments: no tool call is
  // in flight and no block is running, so nothing above claims the frontier.
  // The frames are live-only, which is why a terminated turn never shows this
  // and a reload — whose hydrated turn carries no progress — cannot strand it.
  const drafting = turn.codegenProgress;
  if (!ended && liveIndex === -1 && drafting === null) {
    liveIndex = newestStepIndex(rows);
    if (liveIndex !== -1) rows[liveIndex]!.live = true;
  }
  if (!ended && drafting !== null) {
    rows.push({
      id: "codegen-progress",
      kind: "author",
      entries: [],
      blocks: [],
      codeDiffs: [],
      pending: false,
      live: true,
      reason: null,
      startedAt: null,
      endedAt: null,
      draftingLabels: drafting.blockLabels,
    });
    liveIndex = rows.length - 1;
  }

  // Focus follows the newest row, skipping a trailing sentence with no calls
  // yet, and a contentless draft placeholder never hides the live row.
  let newestIndex = rows.length - 1;
  while (newestIndex > 0 && isReasonOnlyRow(rows[newestIndex]!)) {
    newestIndex -= 1;
  }
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

export function callLabel(entry: ActivityEntry): string {
  return entry.displayLabel ?? toolActivityDisplayLabel(entry.toolName);
}

export interface CondensedCall {
  entry: ActivityEntry;
  count: number;
}

// Consecutive settled calls that read the same and returned the same result
// are one line with a count; a failure or a call still running never merges.
export function condenseCalls(entries: ActivityEntry[]): CondensedCall[] {
  const out: CondensedCall[] = [];
  for (const entry of entries) {
    if (entry.kind === "narration") continue;
    const previous = out[out.length - 1];
    if (
      previous !== undefined &&
      entry.reason === undefined &&
      previous.entry.reason === undefined &&
      entry.kind === "tool_result" &&
      previous.entry.kind === "tool_result" &&
      entry.success !== false &&
      previous.entry.success !== false &&
      entry.toolName === previous.entry.toolName &&
      callLabel(entry) === callLabel(previous.entry) &&
      entry.text === previous.entry.text
    ) {
      previous.count += 1;
      continue;
    }
    out.push({ entry, count: 1 });
  }
  return out;
}

function counted(n: number, one: string, many: string): string {
  return n === 1 ? `1 ${one}` : `${n} ${many}`;
}

// A retry keeps its failed attempts beside it, so a finished step counts only
// the calls that returned a success; one that never did says it tried.
function returnedOk(entry: ActivityEntry): boolean {
  return entry.kind === "tool_result" && entry.success !== false;
}

// A retried call's attempts are one action.
function attemptCount(entries: ActivityEntry[]): number {
  return new Set(entries.map(stableRowId)).size;
}

// Two writes to the same block are one block when their diffs name it.
function blockCount(calls: ActivityEntry[]): number {
  const labels = new Set(
    calls.flatMap((call) => (call.codeDiffs ?? []).map((diff) => diff.label)),
  );
  return labels.size > 0 ? labels.size : attemptCount(calls);
}

function writePhrase(entries: ActivityEntry[], live: boolean): string {
  const parts: string[] = [];
  const blockScoped = (
    tool: string,
    base: string,
    verb: string,
    past: string,
  ) => {
    const calls = entries.filter((entry) => entry.toolName === tool);
    if (calls.length === 0) return 0;
    const ok = calls.filter(returnedOk);
    if (live) {
      const active = calls.filter(
        (entry) => entry.kind === "tool_call" || returnedOk(entry),
      );
      parts.push(
        `${verb} ${counted(Math.max(blockCount(active), 1), "block", "blocks")}`,
      );
    } else if (ok.length > 0) {
      parts.push(`${past} ${counted(blockCount(ok), "block", "blocks")}`);
    } else {
      parts.push(
        `tried to ${base} ${attemptCount(calls) === 1 ? "a block" : "blocks"}`,
      );
    }
    return calls.length;
  };
  const scoped =
    blockScoped("add_block", "add", "adding", "added") +
    blockScoped("edit_block", "edit", "editing", "edited") +
    blockScoped("delete_block", "delete", "deleting", "deleted");
  if (scoped < entries.length) {
    const rest = entries.filter(
      (entry) =>
        !["add_block", "edit_block", "delete_block"].includes(
          entry.toolName ?? "",
        ),
    );
    parts.push(
      live
        ? "updating the workflow"
        : rest.some(returnedOk)
          ? "updated the workflow"
          : "tried to update the workflow",
    );
  }
  return parts.join(", ");
}

function kindPhrase(
  kind: ToolCallKind,
  entries: ActivityEntry[],
  live: boolean,
): string {
  const uses = (name: string) => entries.some((e) => e.toolName === name);
  switch (kind) {
    case "browser":
      return counted(
        attemptCount(entries),
        "browser action",
        "browser actions",
      );
    case "credential":
      if (uses("fill_credential_field")) {
        if (live) return "using a saved login";
        return entries.some(
          (e) => e.toolName === "fill_credential_field" && returnedOk(e),
        )
          ? "used a saved login"
          : "tried a saved login";
      }
      if (uses("request_credential")) {
        return live ? "asking for a login" : "asked for a login";
      }
      return live ? "checking saved logins" : "checked saved logins";
    case "plan":
      return live ? "updating its plan" : "updated its plan";
    case "guidance":
      return live ? "looking up guidance" : "looked up guidance";
    case "write":
      return writePhrase(entries, live);
    case "run":
      if (live) return "testing the workflow";
      return entries.length === 1
        ? "tested the workflow"
        : `tested the workflow ${entries.length} times`;
    case "other": {
      const asks = entries.filter((e) => e.toolName === "ask_user").length;
      const rest = entries.length - asks;
      return [
        asks > 0 ? (live ? "asking you" : "asked you a question") : null,
        rest > 0 ? counted(rest, "other step", "other steps") : null,
      ]
        .filter((part) => part !== null)
        .join(", ");
    }
  }
}

// Names a step by the kinds of call it made, in the order each kind first
// appeared. A kind reads in the present tense while one of its calls is still
// waiting on its result, unless `settled` says the turn is over.
export function callRollup(
  entries: ActivityEntry[],
  settled = false,
): string | null {
  const byKind = new Map<ToolCallKind, ActivityEntry[]>();
  for (const entry of entries) {
    if (entry.kind === "narration" || entry.toolName === undefined) continue;
    const kind = toolCallKind(entry.toolName);
    byKind.set(kind, [...(byKind.get(kind) ?? []), entry]);
  }
  if (byKind.size === 0) return null;
  const text = [...byKind]
    .map(([kind, calls]) =>
      kindPhrase(
        kind,
        calls,
        !settled && calls.some((call) => call.kind === "tool_call"),
      ),
    )
    .join(", ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// The row's blocks keyed by the tool call id of the run call that ran them: the
// first run call, in row order, still running when the block was first seen (its
// start, else its end, since a short block can be first polled finished). A
// block with neither, or seen after every dated call returned, goes to the row's
// last run call. Empty when the row made no run call.
export function blocksByRunCall(row: ActivityRow): Map<string, BlockState[]> {
  const runCalls = row.entries.filter(
    (entry) =>
      entry.toolName !== undefined && toolCallKind(entry.toolName) === "run",
  );
  const byCall = new Map<string, BlockState[]>();
  const lastRun = runCalls[runCalls.length - 1];
  const lastId = lastRun === undefined ? undefined : toolCallIdOf(lastRun);
  if (lastId === undefined) return byCall;
  for (const block of row.blocks) {
    const seenMs = parseUtcIsoMs(block.startedAt ?? block.endedAt);
    const host =
      seenMs === null
        ? undefined
        : runCalls.find((entry) => {
            if (entry.kind === "tool_call") return true;
            const endedMs = parseUtcIsoMs(entry.timestamp);
            return endedMs !== null && endedMs >= seenMs;
          });
    const id = (host && toolCallIdOf(host)) || lastId;
    byCall.set(id, [...(byCall.get(id) ?? []), block]);
  }
  return byCall;
}

export function failedRowBlocks(row: ActivityRow): BlockState[] {
  return row.blocks.filter((block) => block.state === "failed");
}

// A run row whose every block passed, by the same rule its cards use; null otherwise.
export function passedBlockCount(row: ActivityRow): number | null {
  if (row.kind !== "run" || row.pending || row.blocks.length === 0) {
    return null;
  }
  const last = row.entries[row.entries.length - 1];
  if (last?.kind === "tool_result" && last.success === false) return null;
  return row.blocks.every(blockPassed) ? row.blocks.length : null;
}

function isFailedTest(row: ActivityRow): boolean {
  if (row.kind !== "run") return false;
  if (failedRowBlocks(row).length > 0) return true;
  const lastRun = [...row.entries]
    .reverse()
    .find(
      (entry) =>
        entry.toolName !== undefined && toolCallKind(entry.toolName) === "run",
    );
  return lastRun?.kind === "tool_result" && lastRun.success === false;
}

export interface FinishedTurnSummary {
  steps: number;
  failedTests: number;
  // Every failure was followed by a clean run on the current source.
  fixed: boolean;
  // Blocks the newest test left failing, so a folded turn still names them.
  stillFailing: BlockState[];
}

export function summarizeFinishedTurn(
  rows: ActivityRow[],
  turnFacts: TurnFacts | null,
): FinishedTurnSummary {
  const failedTests = rows.filter(isFailedTest).length;
  const fixed = failedTests > 0 && ranCleanOnCurrentSource(turnFacts);
  const newestRun = [...rows].reverse().find((row) => row.kind === "run");
  return {
    steps: rows.filter((row) => !isReasonOnlyRow(row)).length,
    failedTests,
    fixed,
    stillFailing:
      fixed || newestRun === undefined ? [] : failedRowBlocks(newestRun),
  };
}
