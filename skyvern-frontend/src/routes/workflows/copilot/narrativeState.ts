// Pure reducer + types for the workflow copilot turn-narrative bubble. Kept
// separate from NarrativeView.tsx so Vite Fast Refresh can hot-reload the
// component without re-evaluating reducer state, and so the reducer can be
// exercised under vitest without a JSX runtime.

import { buildRevealOffsets } from "./actionReveal";
import {
  CopilotResponseType,
  ProposalDisposition,
  WorkflowCopilotBlockProgressUpdate,
  WorkflowCopilotDesignEndUpdate,
  WorkflowCopilotDesignStartUpdate,
  WorkflowCopilotNarrationUpdate,
  WorkflowCopilotRunOutcomeUpdate,
  WorkflowCopilotStreamErrorUpdate,
  WorkflowCopilotStreamResponseUpdate,
  WorkflowCopilotToolCallUpdate,
  WorkflowCopilotToolResultUpdate,
  WorkflowCopilotTurnStartUpdate,
  WorkflowCopilotWorkflowDraftUpdate,
} from "./workflowCopilotTypes";

// A block's recorded actions, fetched from the run timeline while the test run
// is live (polled from the first frame that carries the run id) and again at
// adjudication. block_progress carries no action detail, only the run id.
export interface RecordedActionSummary {
  actionId: string;
  label: string;
  summary: string | null;
  durationMs: number | null;
  failed: boolean;
}

// Client-synthesized event (never sent by the backend) that carries the
// fetched recorded actions into the reducer so the reveal schedule can be
// derived the same way as every other narrative update.
export interface CopilotBlockActionsEvent {
  type: "client_block_actions";
  blocks: Array<{
    workflowRunBlockId: string;
    actions: RecordedActionSummary[];
  }>;
  receivedAtMs: number;
}

// Client-synthesized event marking that the drafting silence (no frames
// while the LLM writes code) has lasted long enough to assume Draft has
// started. Idempotent in the reducer so a re-armed timer or StrictMode
// double-fire is a no-op.
export interface CopilotPhaseHintEvent {
  type: "client_phase_hint";
  hintedAtMs: number;
}

// Discriminated union of every event the reducer below consumes. The bubble
// derives all of its rendering from these payloads.
export type NarrativeEvent =
  | WorkflowCopilotTurnStartUpdate
  | WorkflowCopilotDesignStartUpdate
  | WorkflowCopilotDesignEndUpdate
  | WorkflowCopilotWorkflowDraftUpdate
  | WorkflowCopilotBlockProgressUpdate
  | WorkflowCopilotRunOutcomeUpdate
  | WorkflowCopilotStreamResponseUpdate
  | WorkflowCopilotStreamErrorUpdate
  | WorkflowCopilotNarrationUpdate
  | WorkflowCopilotToolCallUpdate
  | WorkflowCopilotToolResultUpdate
  | CopilotBlockActionsEvent
  | CopilotPhaseHintEvent;

// Block lifecycle states as observed via block_progress. The bubble groups
// failed-style states (failed, terminated, timed_out, canceled) under one
// chip and treats `skipped` as a separate neutral state.
export type BlockUIState =
  | "queued"
  | "drafted"
  | "running"
  | "completed"
  | "failed"
  | "skipped";

// Recorded per-run outcome verdict, distinct from lifecycle state: a row can
// be `completed` (it ran) yet carry a `not_demonstrated` verdict.
export type BlockOutcome =
  | "evaluating"
  | "demonstrated"
  | "not_demonstrated"
  | "not_evaluated";

export interface TerminalEnvelopeFacts {
  runVerdict: BlockOutcome | null;
  runDisplayReason: string | null;
}

// Envelope dicts are backend model_dump output, so keys stay snake_case.
// The backend anchors run_verdict from final outcomes only, so "evaluating"
// is not a wire value here and parses to null like any unknown.
export function parseTerminalEnvelope(
  raw: unknown,
): TerminalEnvelopeFacts | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  const v = obj.run_verdict;
  return {
    runVerdict:
      v === "demonstrated" || v === "not_demonstrated" || v === "not_evaluated"
        ? v
        : null,
    runDisplayReason:
      typeof obj.run_display_reason === "string"
        ? obj.run_display_reason
        : null,
  };
}

export interface BlockState {
  workflowRunBlockId: string;
  label: string;
  blockType: string;
  state: BlockUIState;
  // Undefined when no verdict was recorded (older backend or unadjudicated run).
  outcome?: BlockOutcome;
  outcomeReason?: string;
  lastSeenIteration: number;
  // Tool calls and results emitted while this block was running are
  // appended here so the expanded card shows what the agent did.
  activity: ActivityEntry[];
  // ISO timestamps captured the first time the block was seen running and
  // the first time it landed in a terminal state. Drives the per-block
  // ``done · 0:14``-style elapsed pill in the card.
  startedAt: string | null;
  endedAt: string | null;
  // Recorded actions for progressive reveal. Undefined until the first fetch
  // resolves; grows by actionId as live polls return more (idempotent merge).
  recordedActions?: RecordedActionSummary[];
  // Epoch ms this block's reveal schedule starts counting from — staggered
  // past preceding blocks' schedules so a multi-block run reveals in order.
  recordedActionsAt?: number;
}

export interface ActivityEntry {
  kind: "tool_call" | "tool_result" | "narration";
  // Free-text label rendered as a one-line summary in the card body.
  text: string;
  // Iteration of the agent loop that emitted the event. Used to dedupe
  // re-sent payloads and to align tool_result with its tool_call.
  iteration: number;
  // Tool name when kind is tool_call/tool_result.
  toolName?: string;
  // Product-safe label for rendering tool activity to users.
  displayLabel?: string;
  // Result success when kind is tool_result.
  success?: boolean;
  // Stable per-event id used as React key.
  id: string;
  // Consecutive same-tool retries folded into this row by
  // condenseActivityEntries. Unset outside that transform.
  attempts?: number;
}

// Closed vocabulary of the backend TurnOutcome.response_kind enum. Unknown
// wire values parse to null so a newer backend cannot crash the renderer.
export type TurnResponseKind =
  | "build"
  | "clarify"
  | "diagnose"
  | "refuse"
  | "recover";

export interface TurnNarrativeState {
  turnId: string | null;
  turnIndex: number | null;
  mode: string;
  responseType: CopilotResponseType | null;
  proposalDisposition: ProposalDisposition | null;
  // Typed terminal adjudication of the turn (TurnOutcome.response_kind).
  // Null on legacy rows and frames from an older backend.
  responseKind: TurnResponseKind | null;
  // Outcome-evidence verdict authorizing tested-success claims (ADR 0005).
  // Null means unknown (legacy/grafted rows) — distinct from false.
  verifiedSuccess: boolean | null;
  // Run-outcome facts from the backend-finalized terminal envelope carried
  // in the narrative payload. Authoritative once runVerdict is set; null on
  // rows persisted before the envelope existed.
  terminalEnvelope: TerminalEnvelopeFacts | null;
  designStarted: boolean;
  designEnded: boolean;
  draft: {
    blockCount: number;
    blockLabels: string[];
    summary: string | null;
  } | null;
  blocks: BlockState[];
  terminal: "response" | "error" | null;
  terminalMessage: string | null;
  narrativeSummary: string | null;
  cancelled: boolean;
  // Block count of the canonical workflow at turn entry. Drives the edit-
  // vs-build chip derivation; the snap-back source is captured client-side
  // at submit so unsaved local canvas edits survive.
  priorBlockCount: number | null;
  // ISO timestamps from turn_start and the terminal frame. Drive the convo
  // aggregate pill's elapsed display across all turns.
  startedAt: string | null;
  endedAt: string | null;
  // Activity events fired BEFORE any block started running this turn
  // (design phase + pre-execution tool calls), rendered inside the Design card.
  designActivity: ActivityEntry[];
  // Client-only phase-progress state (never persisted): epoch ms of the most
  // recent tool_call/tool_result/narration, and when the 8s drafting-gap
  // heuristic fired. Grafted across the terminal payload swap by turnId so a
  // cancel-mid-silence doesn't visually un-check the Draft phase.
  lastActivityAtMs: number | null;
  draftingSignaledAt: number | null;
  // Count of AUTHORING_TOOLS tool_calls this turn, kept outside designActivity
  // so it survives the MAX_DESIGN_ACTIVITY_ENTRIES eviction cap. Drives the
  // redraft-iteration label ("Draft v2 — revising…") and Draft re-activation.
  authoringCount: number;
  // Monotonic count of tool_call events this turn — the only frame kind
  // that's unambiguous evidence of new agent-initiated work (tool_result is
  // always a trailing echo; narration can be scheduled as post-hoc
  // reporting independent of new work — see the reducer cases). Never
  // capped (unlike designActivity.length, which plateaus once
  // MAX_DESIGN_ACTIVITY_ENTRIES is full).
  activitySeq: number;
  // Snapshot of the most recent run_outcome verdict plus the activity
  // sequence number at the moment it arrived, so a later activity frame
  // proves the loop kept going (a redraft), not just a slow give-up
  // response. Not grafted across terminal — cancel-mid-redraft marks Test
  // stopped rather than Draft (accepted).
  lastRunOutcome: {
    verdict: BlockOutcome;
    displayReason: string | null;
    activitySeqAtVerdict: number;
  } | null;
  // Terminal-mode credential ask, from the credentialPrompt narrative signal.
  // reason is kept as a raw string — the card tolerates unknown tokens.
  credentialPrompt: { reason: string } | null;
  // Resolved pause outcome, from the credentialPause narrative signal.
  // "declined" means the pause engaged but never sent a frame, so no card.
  credentialPause: {
    outcome: "connected" | "skipped" | "timeout" | "declined";
    credentialId: string | null;
  } | null;
}

export const EMPTY_NARRATIVE: TurnNarrativeState = Object.freeze({
  turnId: null,
  turnIndex: null,
  mode: "unknown",
  responseType: null,
  proposalDisposition: null,
  responseKind: null,
  verifiedSuccess: null,
  terminalEnvelope: null,
  designStarted: false,
  designEnded: false,
  draft: null,
  blocks: [],
  terminal: null,
  terminalMessage: null,
  narrativeSummary: null,
  cancelled: false,
  priorBlockCount: null,
  startedAt: null,
  endedAt: null,
  designActivity: [],
  lastActivityAtMs: null,
  draftingSignaledAt: null,
  authoringCount: 0,
  activitySeq: 0,
  lastRunOutcome: null,
  credentialPrompt: null,
  credentialPause: null,
}) as TurnNarrativeState;

// Caps to keep long-running narrations from unbounded growth (and to keep
// the rendered card from becoming a wall of text).
const MAX_ACTIVITY_ENTRIES = 30;
const MAX_DESIGN_ACTIVITY_ENTRIES = 50;

// Some BE paths emit naive ISO datetimes (no timezone offset), e.g. the
// chat-history endpoint serializing SQLAlchemy created_at columns. JS
// Date.parse interprets those as local time, producing a multi-hour
// drift versus timestamps that ARE timezone-aware (narrative_payload
// startedAt/endedAt). Normalize to UTC when the offset is absent.
export function parseUtcIsoMs(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const hasTz = /Z$|[+-]\d{2}:?\d{2}$/.test(iso);
  const ms = Date.parse(hasTz ? iso : `${iso}+00:00`);
  return Number.isFinite(ms) ? ms : null;
}

export function parseResponseKind(value: unknown): TurnResponseKind | null {
  return value === "build" ||
    value === "clarify" ||
    value === "diagnose" ||
    value === "refuse" ||
    value === "recover"
    ? value
    : null;
}

export function parseCredentialPrompt(
  value: unknown,
): TurnNarrativeState["credentialPrompt"] {
  if (!value || typeof value !== "object") return null;
  const reason = (value as Record<string, unknown>).reason;
  return typeof reason === "string" && reason.length > 0 ? { reason } : null;
}

export function parseCredentialPause(
  value: unknown,
): TurnNarrativeState["credentialPause"] {
  if (!value || typeof value !== "object") return null;
  const o = value as Record<string, unknown>;
  const outcome = o.outcome;
  if (
    outcome !== "connected" &&
    outcome !== "skipped" &&
    outcome !== "timeout" &&
    outcome !== "declined"
  ) {
    return null;
  }
  return {
    outcome,
    credentialId: typeof o.credentialId === "string" ? o.credentialId : null,
  };
}

// Tool calls that write the workflow definition. update_workflow only
// validates/saves the draft; update_and_run_blocks also runs it, so it's
// the one AUTHORING_TOOLS member that's also a RUN_TOOLS member (its
// activity lands in the Test phase bucket, not Draft — see copilotPhases.ts).
export const AUTHORING_TOOLS = new Set([
  "update_workflow",
  "update_and_run_blocks",
]);
export const RUN_TOOLS = new Set([
  "update_and_run_blocks",
  "run_blocks_and_collect_debug",
]);

// Tool names we never surface in the user-facing activity log. Internal
// observation/maintenance tools are not interesting context for the user.
const ACTIVITY_TOOL_DENYLIST = new Set([
  "list_credentials",
  "get_run_results",
  "get_browser_screenshot",
]);

// Mirror of the backend _TOOL_ACTIVITY_DISPLAY_LABELS in narration.py.
const ACTIVITY_TOOL_DISPLAY_LABELS: Record<string, string> = {
  update_workflow: "Updating workflow",
  update_and_run_blocks: "Testing workflow",
  run_blocks_and_collect_debug: "Testing workflow",
  evaluate: "Inspecting page",
  click: "Interacting with page",
  type_text: "Entering text",
  scroll: "Interacting with page",
  select_option: "Selecting option",
  press_key: "Interacting with page",
  navigate_browser: "Opening page",
  get_block_schema: "Checking workflow block options",
  inspect_current_workflow: "Inspecting workflow",
  discover_workflow_entrypoint: "Finding the entry page",
  inspect_page_for_composition: "Inspecting the page",
};

export function toolActivityDisplayLabel(toolName?: string | null): string {
  if (!toolName) return "Working";
  return ACTIVITY_TOOL_DISPLAY_LABELS[toolName] ?? "Working";
}

function buildActivityFromToolCall(
  event: WorkflowCopilotToolCallUpdate,
): ActivityEntry | null {
  if (ACTIVITY_TOOL_DENYLIST.has(event.tool_name)) {
    return null;
  }
  const displayLabel =
    event.display_label ?? toolActivityDisplayLabel(event.tool_name);
  return {
    kind: "tool_call",
    text: `${displayLabel}…`,
    iteration: event.iteration,
    toolName: event.tool_name,
    displayLabel,
    id: `tc-${event.tool_call_id}`,
  };
}

function buildActivityFromToolResult(
  event: WorkflowCopilotToolResultUpdate,
): ActivityEntry | null {
  if (ACTIVITY_TOOL_DENYLIST.has(event.tool_name)) {
    return null;
  }
  const displayLabel = toolActivityDisplayLabel(event.tool_name);
  return {
    kind: "tool_result",
    text: event.summary || displayLabel,
    iteration: event.iteration,
    toolName: event.tool_name,
    displayLabel,
    success: event.success,
    id: `tr-${event.tool_call_id}`,
  };
}

function buildActivityFromNarration(
  event: WorkflowCopilotNarrationUpdate,
): ActivityEntry {
  return {
    kind: "narration",
    text: event.narration,
    iteration: event.iteration,
    id: `n-${event.iteration}-${event.timestamp}`,
  };
}

// Shared "tc-<tool_call_id>" / "tr-<tool_call_id>" id-parsing convention —
// also used by copilotPhases.ts's hasPendingToolCall.
export function toolCallIdOf(entry: ActivityEntry): string | undefined {
  return entry.kind === "tool_call" || entry.kind === "tool_result"
    ? entry.id.slice(3)
    : undefined;
}

// Folds each tool_call/tool_result pair into one row (pending while
// unresolved, replaced by its result once it lands) so a single tool
// invocation never renders as two chatter lines, then folds a run of
// same-tool retries into the last attempt's row with an attempt count —
// only the terminal outcome of a retry chain ever shows red, intermediate
// failed attempts stay quiet.
//
// Chronology, not just row count: a call is only replaced IN PLACE when
// nothing else streamed while it was pending. If a narration arrived first
// (the narrator can emit mid-flight progress on a slow call), replacing in
// place would render the result ahead of a narration that genuinely came
// earlier — so the stale pending row is dropped instead and the result
// lands at its own later, true position.
//
// The retry fold below deliberately ignores narration rows entirely for
// adjacency (tracks the last TOOL row, not literal array adjacency).
// Array position alone can't reliably tell "narration mid-flight during
// this attempt" from "narration genuinely between two attempts" once a
// retry's own narration is involved — a mid-flight narration during
// attempt 2 lands in the exact same ordered position as one that arrived
// between attempt 1 and attempt 2. Since there's no per-entry signal to
// disambiguate the two, narration never breaks a retry fold, full stop.
// Folding itself follows the same drop-and-reinsert rule as pairing above:
// the earlier attempt's row is removed rather than overwritten in place,
// so a narration that streamed before the later attempt's result still
// reads as arriving before it, not after.
//
// Known tradeoff: toolName is the only correlation signal available here
// (no argument/target identity on the wire), so two independent same-tool
// calls where only the first fails will also fold into one falsely-labeled
// "retry" row. Accepted for this content-classification pass.
//
// A tool without a dedicated backend summary falls back to a bare "OK"
// (summarize_tool_result in output_utils.py). That used to sit right below
// its own "<tool name> · calling…" row, so the tool name was still visible
// above it; condensing removes that row, leaving an unlabeled "OK" with no
// context. Substitute the humanized tool name only for that exact literal
// — a real backend summary always passes through untouched.
const UNMAPPED_TOOL_RESULT_FALLBACK = "OK";

function withHumanizedFallback(entry: ActivityEntry): ActivityEntry {
  if (
    entry.kind !== "tool_result" ||
    entry.text !== UNMAPPED_TOOL_RESULT_FALLBACK
  ) {
    return entry;
  }
  return {
    ...entry,
    text: entry.displayLabel ?? toolActivityDisplayLabel(entry.toolName),
  };
}

export function condenseActivityEntries(
  entries: ActivityEntry[],
): ActivityEntry[] {
  const callIndexById = new Map<string, number>();
  const paired: (ActivityEntry | null)[] = [];
  for (const entry of entries) {
    if (entry.kind === "tool_call") {
      const id = toolCallIdOf(entry);
      const idx = paired.push(entry) - 1;
      if (id !== undefined) callIndexById.set(id, idx);
      continue;
    }
    if (entry.kind === "tool_result") {
      const id = toolCallIdOf(entry);
      const idx = id !== undefined ? callIndexById.get(id) : undefined;
      if (idx !== undefined) {
        callIndexById.delete(id!);
        if (idx === paired.length - 1) {
          paired[idx] = entry;
        } else {
          paired[idx] = null;
          paired.push(entry);
        }
      } else {
        // Its tool_call was evicted past the activity cap — keep the
        // result visible rather than silently dropping it.
        paired.push(entry);
      }
      continue;
    }
    paired.push(entry);
  }
  const ordered = paired.filter((e): e is ActivityEntry => e !== null);

  const condensed: (ActivityEntry | null)[] = [];
  let lastToolIdx = -1;
  for (const entry of ordered) {
    const prevTool = lastToolIdx >= 0 ? condensed[lastToolIdx] : undefined;
    if (
      prevTool &&
      entry.toolName !== undefined &&
      prevTool.toolName === entry.toolName &&
      prevTool.success === false
    ) {
      condensed[lastToolIdx] = null;
      lastToolIdx =
        condensed.push({
          ...entry,
          attempts: (prevTool.attempts ?? 1) + 1,
        }) - 1;
      continue;
    }
    const idx = condensed.push(entry) - 1;
    if (entry.toolName !== undefined) {
      lastToolIdx = idx;
    }
  }
  return condensed
    .filter((e): e is ActivityEntry => e !== null)
    .map(withHumanizedFallback);
}

function appendCapped<T>(arr: T[], entry: T, cap: number): T[] {
  const next = [...arr, entry];
  return next.length > cap ? next.slice(next.length - cap) : next;
}

function appendActivity(
  blocks: BlockState[],
  designActivity: ActivityEntry[],
  entry: ActivityEntry,
): { blocks: BlockState[]; designActivity: ActivityEntry[] } {
  // A run tool's result must rejoin its call's bucket. The run flips the active
  // block between the call and the result, so routing the result to the live
  // active block would split the call/result pair across buckets and it could
  // never fold (mirrors the backend NarratorState._activity_bucket_label fix).
  if (
    entry.kind === "tool_result" &&
    entry.toolName !== undefined &&
    RUN_TOOLS.has(entry.toolName)
  ) {
    const callId = `tc-${toolCallIdOf(entry) ?? ""}`;
    if (designActivity.some((e) => e.id === callId)) {
      return {
        blocks,
        designActivity: appendCapped(
          designActivity,
          entry,
          MAX_DESIGN_ACTIVITY_ENTRIES,
        ),
      };
    }
    const callBlockIdx = blocks.findIndex((b) =>
      b.activity.some((e) => e.id === callId),
    );
    if (callBlockIdx !== -1) {
      const nextBlocks = blocks.slice();
      const callBlock = nextBlocks[callBlockIdx]!;
      nextBlocks[callBlockIdx] = {
        ...callBlock,
        activity: appendCapped(callBlock.activity, entry, MAX_ACTIVITY_ENTRIES),
      };
      return { blocks: nextBlocks, designActivity };
    }
  }
  const activeIdx = blocks.findIndex((b) => b.state === "running");
  if (activeIdx === -1) {
    return {
      blocks,
      designActivity: appendCapped(
        designActivity,
        entry,
        MAX_DESIGN_ACTIVITY_ENTRIES,
      ),
    };
  }
  const nextBlocks = blocks.slice();
  const active = nextBlocks[activeIdx]!;
  nextBlocks[activeIdx] = {
    ...active,
    activity: appendCapped(active.activity, entry, MAX_ACTIVITY_ENTRIES),
  };
  return { blocks: nextBlocks, designActivity };
}

export function mapBlockStatus(raw: string): BlockUIState {
  switch (raw) {
    case "running":
      return "running";
    case "completed":
      return "completed";
    case "drafted":
      return "drafted";
    case "failed":
    case "terminated":
    case "timed_out":
    case "canceled":
      return "failed";
    case "skipped":
      return "skipped";
    case "queued":
    default:
      return "queued";
  }
}

// A completed row claims the success affordance only when no recorded verdict
// contradicts it; `evaluating` and `not_demonstrated` both withhold the check.
export function isBlockOk(
  block: Pick<BlockState, "state" | "outcome">,
): boolean {
  if (block.state !== "completed") return false;
  return (
    block.outcome === undefined ||
    block.outcome === "demonstrated" ||
    block.outcome === "not_evaluated"
  );
}

// Labels that occur exactly once in the given set — the only labels safe to
// key recorded actions on when the run-block id is missing (the terminal
// narrative_payload drops workflowRunBlockId). Loop iterations reuse a label,
// so an ambiguous label falls back to today's drop rather than mis-attributing.
function uniqueLabelSet(labels: Array<string | undefined>): Set<string> {
  const counts = new Map<string, number>();
  for (const label of labels) {
    if (!label) continue;
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  const unique = new Set<string>();
  for (const [label, n] of counts) if (n === 1) unique.add(label);
  return unique;
}

export function applyNarrativeEvent(
  prev: TurnNarrativeState,
  event: NarrativeEvent,
  nowMs: number = Date.now(),
): TurnNarrativeState {
  switch (event.type) {
    case "turn_start":
      return {
        ...EMPTY_NARRATIVE,
        turnId: event.turn_id,
        turnIndex: event.turn_index,
        mode: event.mode || "unknown",
        priorBlockCount: event.prior_block_count ?? null,
        startedAt: event.timestamp ?? null,
      };

    case "design_start":
      return { ...prev, designStarted: true };

    case "design_end":
      return { ...prev, designEnded: true };

    case "workflow_draft": {
      // Bubble shows the summary fields; canvas mid-turn rendering of
      // ``event.workflow`` is wired in WorkflowCopilotChat.tsx. Seed
      // ``blocks`` from block_labels so each drafted block renders its own
      // card even on draft-only turns (where no block_progress fires).
      // Existing entries from prior block_progress events take precedence;
      // new labels join as ``drafted`` until block_progress upgrades them.
      const labelToExisting = new Map(prev.blocks.map((b) => [b.label, b]));
      const nextBlocks: BlockState[] = event.block_labels.map((label) => {
        const existing = labelToExisting.get(label);
        if (existing) return existing;
        return {
          workflowRunBlockId: "",
          label,
          blockType: "task",
          state: "drafted",
          lastSeenIteration: 0,
          activity: [],
          startedAt: null,
          endedAt: null,
        };
      });
      // Preserve any prior block whose label was dropped from the draft (rare
      // — happens if the agent renames a block mid-turn). Drop those that no
      // longer exist; they no longer participate in the proposal.
      return {
        ...prev,
        draft: {
          blockCount: event.block_count,
          blockLabels: event.block_labels,
          summary: event.summary,
        },
        blocks: nextBlocks,
      };
    }

    case "block_progress": {
      const incomingState = mapBlockStatus(event.status);
      // Key on workflow_run_block_id, not block_label, so loop iterations
      // (e.g. a for_loop body) render as distinct rows.
      let existing = prev.blocks.findIndex(
        (b) => b.workflowRunBlockId === event.workflow_run_block_id,
      );
      // A drafted placeholder seeded by workflow_draft has no run-block id
      // yet; upgrade it in place on its first block_progress rather than
      // spawning a duplicate row.
      if (existing < 0) {
        existing = prev.blocks.findIndex(
          (b) =>
            b.workflowRunBlockId === "" &&
            b.state === "drafted" &&
            b.label === event.block_label,
        );
      }
      const eventTs = event.timestamp ?? null;
      const previousBlock = existing >= 0 ? prev.blocks[existing]! : null;
      const startedAt =
        previousBlock?.startedAt ??
        (incomingState === "running" ? eventTs : null);
      const isTerminal =
        incomingState === "completed" ||
        incomingState === "failed" ||
        incomingState === "skipped";
      // Clear endedAt on retry-back-to-running so the elapsed pill doesn't
      // show stale "DONE · 2:00" while the block is active again. On each
      // terminal event take the latest wall clock so a retry-then-succeed
      // sequence reflects the final completion time, not the first failure.
      const endedAt = isTerminal ? eventTs : null;
      const baseEntry: BlockState = {
        workflowRunBlockId: event.workflow_run_block_id,
        label: event.block_label,
        blockType: event.block_type,
        state: incomingState,
        // A late or replayed block_progress must not wipe a recorded verdict
        // or an already-fetched action replay; lifecycle frames never carry
        // either, so always keep the prior values.
        outcome: previousBlock?.outcome,
        outcomeReason: previousBlock?.outcomeReason,
        recordedActions: previousBlock?.recordedActions,
        recordedActionsAt: previousBlock?.recordedActionsAt,
        lastSeenIteration: event.iteration,
        activity: previousBlock?.activity ?? [],
        startedAt,
        endedAt,
      };
      if (existing >= 0) {
        const nextBlocks = prev.blocks.slice();
        nextBlocks[existing] = baseEntry;
        return { ...prev, blocks: nextBlocks };
      }
      return { ...prev, blocks: [...prev.blocks, baseEntry] };
    }

    case "run_outcome": {
      // Apply the recorded verdict by run-block id only — no label fallback, so
      // a prior run's rows in the same turn keep their own verdict.
      const ids = new Set(event.workflow_run_block_ids);
      const blocks = prev.blocks.map((b) =>
        ids.has(b.workflowRunBlockId)
          ? {
              ...b,
              outcome: event.verdict,
              outcomeReason: event.display_reason ?? undefined,
            }
          : b,
      );
      return {
        ...prev,
        blocks,
        // Last-write-wins: an "evaluating" hold overwrites a stale failed
        // verdict from a prior run cycle within the same turn.
        lastRunOutcome: {
          verdict: event.verdict,
          displayReason: event.display_reason ?? null,
          activitySeqAtVerdict: prev.activitySeq,
        },
      };
    }

    case "client_block_actions": {
      // Idempotent grow-merge: live polling re-fetches the same run repeatedly
      // and returns a growing action set. First sighting seeds the reveal
      // anchor (recordedActionsAt), staggered past preceding blocks' schedules
      // so a multi-block run replays in execution order. Later fetches append
      // only actions we haven't seen (keyed by actionId) and keep the anchor
      // fixed, so already-revealed rows never restart or duplicate.
      // ponytail: an existing action's fields (duration/summary) are frozen at
      // first sighting; if code-block streaming (null-then-backfill) makes that
      // visibly wrong, re-merge matched actionIds instead of skipping them.
      let carry = 0;
      let changed = false;
      // Match by run-block id only. A terminal frame restores each block's real
      // id from the live blocks (see the `response` case), so a post-terminal
      // fetch still matches by id — and a different run's ids never collide with
      // this turn's, keeping late fetches from cross-contaminating prior turns.
      const blocks = prev.blocks.map((b) => {
        const match = event.blocks.find(
          (entry) => entry.workflowRunBlockId === b.workflowRunBlockId,
        );
        if (!match || match.actions.length === 0) return b;
        const existing = b.recordedActions;
        if (existing === undefined) {
          changed = true;
          const recordedActionsAt = event.receivedAtMs + carry;
          const offsets = buildRevealOffsets(
            match.actions.map((a) => a.durationMs),
          );
          carry += offsets[offsets.length - 1] ?? 0;
          return { ...b, recordedActions: match.actions, recordedActionsAt };
        }
        const known = new Set(existing.map((a) => a.actionId));
        const additions = match.actions.filter((a) => !known.has(a.actionId));
        if (additions.length === 0) return b;
        changed = true;
        return { ...b, recordedActions: [...existing, ...additions] };
      });
      return changed ? { ...prev, blocks } : prev;
    }

    case "tool_call": {
      const entry = buildActivityFromToolCall(event);
      const authoringCount =
        prev.authoringCount + (AUTHORING_TOOLS.has(event.tool_name) ? 1 : 0);
      const activitySeq = prev.activitySeq + 1;
      if (!entry) {
        return {
          ...prev,
          lastActivityAtMs: nowMs,
          authoringCount,
          activitySeq,
        };
      }
      const { blocks, designActivity } = appendActivity(
        prev.blocks,
        prev.designActivity,
        entry,
      );
      return {
        ...prev,
        blocks,
        designActivity,
        lastActivityAtMs: nowMs,
        authoringCount,
        activitySeq,
      };
    }

    case "tool_result": {
      // Deliberately does NOT bump activitySeq: a failed run's own
      // update_and_run_blocks call always emits its trailing tool_result
      // right after the run_outcome verdict it produced — counting that
      // guaranteed echo would make redrafting fire on every failed verdict
      // before the agent has done any new work. Only tool_call/narration
      // (agent-initiated steps) count as evidence the loop continued.
      const entry = buildActivityFromToolResult(event);
      if (!entry) return { ...prev, lastActivityAtMs: nowMs };
      const { blocks, designActivity } = appendActivity(
        prev.blocks,
        prev.designActivity,
        entry,
      );
      return {
        ...prev,
        blocks,
        designActivity,
        lastActivityAtMs: nowMs,
      };
    }

    case "narration": {
      // Also does NOT bump activitySeq: the narrator can schedule a
      // "reporting on what just happened" narration right after a failed
      // run's own tool_result (streaming_adapter.py schedule_narration),
      // independent of whether the agent is actually going to revise —
      // only a genuinely new tool_call is unambiguous evidence of that.
      const entry = buildActivityFromNarration(event);
      const { blocks, designActivity } = appendActivity(
        prev.blocks,
        prev.designActivity,
        entry,
      );
      return {
        ...prev,
        blocks,
        designActivity,
        lastActivityAtMs: nowMs,
      };
    }

    case "client_phase_hint": {
      // No-op once drafting is already signaled or the turn has moved past
      // pure exploration — idempotent by construction so a re-armed timer or
      // a StrictMode double-fire never overwrites an earlier timestamp.
      if (
        prev.draftingSignaledAt !== null ||
        prev.draft !== null ||
        prev.designEnded ||
        prev.blocks.some((b) => b.state !== "drafted")
      ) {
        return prev;
      }
      return { ...prev, draftingSignaledAt: event.hintedAtMs };
    }

    case "response": {
      const hydrated = hydrateNarrativeFromPayload(event.narrative_payload);
      if (hydrated) {
        // The BE narrative_payload drops workflowRunBlockId and the client-only
        // recordedActions. Re-associate each hydrated block with the live block
        // of the same label to restore its real run-block id (and carry any
        // recordedActions). Restoring the real id keeps this frozen turn keyed
        // by id, so a later test run that reuses a label matches by id and can't
        // graft its actions onto this turn. Unique labels only — loop iterations
        // reuse a label and can't be told apart without the id.
        const liveById = new Map(
          prev.blocks
            .filter((b) => b.workflowRunBlockId !== "")
            .map((b) => [b.workflowRunBlockId, b] as const),
        );
        const uniqueLiveLabels = uniqueLabelSet(
          prev.blocks.map((b) => b.label),
        );
        const uniqueHydratedLabels = uniqueLabelSet(
          hydrated.blocks.map((b) => b.label),
        );
        const liveByLabel = new Map(
          prev.blocks
            .filter((b) => uniqueLiveLabels.has(b.label))
            .map((b) => [b.label, b] as const),
        );
        const blocks = hydrated.blocks.map((b) => {
          const live =
            (b.workflowRunBlockId !== ""
              ? liveById.get(b.workflowRunBlockId)
              : undefined) ??
            (b.workflowRunBlockId === "" && uniqueHydratedLabels.has(b.label)
              ? liveByLabel.get(b.label)
              : undefined);
          if (!live) return b;
          return {
            ...b,
            workflowRunBlockId: live.workflowRunBlockId,
            recordedActions: live.recordedActions,
            recordedActionsAt: live.recordedActionsAt,
          };
        });
        return {
          ...hydrated,
          blocks,
          // Graft across the terminal replacement so a cancel mid-silence
          // doesn't visually un-check the Draft phase (hydrated payloads
          // never carry this client-only field). authoringCount/lastRunOutcome
          // are intentionally NOT grafted — a stubs-only terminal checklist is
          // correct there.
          draftingSignaledAt:
            hydrated.turnId === prev.turnId ? prev.draftingSignaledAt : null,
          responseType: event.response_type ?? hydrated.responseType,
          cancelled: event.cancelled ?? hydrated.cancelled,
          proposalDisposition:
            event.proposal_disposition ?? hydrated.proposalDisposition,
          terminalMessage: hydrated.terminalMessage ?? event.message,
          narrativeSummary:
            hydrated.narrativeSummary ??
            event.narrative_summary ??
            event.message,
          endedAt: hydrated.endedAt ?? event.response_time ?? prev.endedAt,
        };
      }

      // Close the design phase on terminal even when DESIGN_END never
      // arrived (a refusal turn can emit DESIGN_START via first
      // tool_called and then exit without persisting a workflow).
      // Some block_progress emissions only fire `running` without a
      // terminal status; resolve any still-running block to `completed`
      // so the chat doesn't show a stuck spinner after the turn is done.
      const terminalTs = event.response_time ?? null;
      const blocks = prev.blocks.map((b) =>
        b.state === "running"
          ? { ...b, state: "completed" as BlockUIState, endedAt: terminalTs }
          : b,
      );
      return {
        ...prev,
        responseType: event.response_type ?? prev.responseType,
        cancelled: event.cancelled ?? prev.cancelled,
        proposalDisposition:
          event.proposal_disposition ?? prev.proposalDisposition,
        designEnded: true,
        terminal: "response",
        terminalMessage: event.message,
        narrativeSummary: event.narrative_summary ?? event.message,
        endedAt: terminalTs ?? prev.endedAt,
        blocks,
      };
    }

    case "error": {
      // Same sweep as the response case: any block stuck in `running` at
      // terminal gets resolved so the chat doesn't show a stuck spinner.
      // On an error terminal we mark them `failed` rather than `completed`.
      const wallClock = prev.endedAt ?? new Date().toISOString();
      const blocks = prev.blocks.map((b) =>
        b.state === "running"
          ? { ...b, state: "failed" as BlockUIState, endedAt: wallClock }
          : b,
      );
      return {
        ...prev,
        designEnded: true,
        terminal: "error",
        terminalMessage: event.error,
        narrativeSummary: event.narrative_summary ?? null,
        endedAt: wallClock,
        blocks,
      };
    }

    default:
      // Exhaustiveness guard — any new NarrativeEvent variant added to the
      // union must extend this switch.
      return prev;
  }
}

function normalizeActivityEntries(raw: unknown): ActivityEntry[] {
  if (!Array.isArray(raw)) return [];
  const out: ActivityEntry[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const o = item as Record<string, unknown>;
    const kind = o.kind;
    if (
      kind !== "tool_call" &&
      kind !== "tool_result" &&
      kind !== "narration"
    ) {
      continue;
    }
    if (typeof o.id !== "string" || typeof o.text !== "string") continue;
    out.push({
      kind,
      text: o.text,
      iteration: typeof o.iteration === "number" ? o.iteration : 0,
      toolName: typeof o.toolName === "string" ? o.toolName : undefined,
      displayLabel:
        typeof o.displayLabel === "string" ? o.displayLabel : undefined,
      success: typeof o.success === "boolean" ? o.success : undefined,
      id: o.id,
    });
  }
  return out;
}

export function hydrateNarrativeFromPayload(
  payload: Record<string, unknown> | null | undefined,
): TurnNarrativeState | undefined {
  if (!payload || typeof payload !== "object") return undefined;
  const turnId = typeof payload.turnId === "string" ? payload.turnId : null;
  // turnIndex can be null on recovered route-error turns where the helper
  // building the payload had no chat-message scope; default to 0 so the
  // turn still hydrates and joins the convo-aggregate accounting.
  const turnIndex =
    typeof payload.turnIndex === "number" ? payload.turnIndex : 0;
  if (turnId === null) return undefined;

  const mode = typeof payload.mode === "string" ? payload.mode : "unknown";
  const rawResponseType = payload.responseType;
  const responseType: CopilotResponseType | null =
    rawResponseType === "REPLY" ||
    rawResponseType === "ASK_QUESTION" ||
    rawResponseType === "REPLACE_WORKFLOW"
      ? rawResponseType
      : null;
  const rawProposalDisposition = payload.proposalDisposition;
  const proposalDisposition: ProposalDisposition | null =
    rawProposalDisposition === "no_proposal" ||
    rawProposalDisposition === "auto_applicable" ||
    rawProposalDisposition === "review_untested" ||
    rawProposalDisposition === "review_tested"
      ? rawProposalDisposition
      : null;
  const draftRaw = payload.draft;
  const draft =
    draftRaw && typeof draftRaw === "object"
      ? {
          blockCount:
            typeof (draftRaw as Record<string, unknown>).blockCount === "number"
              ? ((draftRaw as Record<string, unknown>).blockCount as number)
              : 0,
          blockLabels: Array.isArray(
            (draftRaw as Record<string, unknown>).blockLabels,
          )
            ? ((draftRaw as Record<string, unknown>).blockLabels as string[])
            : [],
          summary:
            typeof (draftRaw as Record<string, unknown>).summary === "string"
              ? ((draftRaw as Record<string, unknown>).summary as string)
              : null,
        }
      : null;

  const blocksRaw = Array.isArray(payload.blocks) ? payload.blocks : [];
  const blocks: BlockState[] = blocksRaw.map((b) => {
    const obj = b as Record<string, unknown>;
    const outcome = ((): BlockOutcome | undefined => {
      const o = obj.outcome;
      if (
        o === "evaluating" ||
        o === "demonstrated" ||
        o === "not_demonstrated" ||
        o === "not_evaluated"
      )
        return o;
      return undefined;
    })();
    return {
      workflowRunBlockId:
        typeof obj.workflowRunBlockId === "string"
          ? obj.workflowRunBlockId
          : "",
      label: typeof obj.label === "string" ? obj.label : "",
      blockType: typeof obj.blockType === "string" ? obj.blockType : "task",
      outcome,
      outcomeReason:
        typeof obj.outcomeReason === "string" ? obj.outcomeReason : undefined,
      state: ((): BlockState["state"] => {
        const s = obj.state;
        if (
          s === "queued" ||
          s === "drafted" ||
          s === "running" ||
          s === "completed" ||
          s === "failed" ||
          s === "skipped"
        )
          return s;
        return "queued";
      })(),
      lastSeenIteration:
        typeof obj.lastSeenIteration === "number" ? obj.lastSeenIteration : 0,
      activity: normalizeActivityEntries(obj.activity),
      startedAt: typeof obj.startedAt === "string" ? obj.startedAt : null,
      endedAt: typeof obj.endedAt === "string" ? obj.endedAt : null,
    };
  });

  const terminal = ((): TurnNarrativeState["terminal"] => {
    const t = payload.terminal;
    if (t === "response" || t === "error") return t;
    return null;
  })();

  // Hydrated payloads sometimes record a block as `running` when the BE
  // dropped its terminal `block_progress`. If the turn itself is terminal,
  // sweep any still-running block to a sensible final state so the UI
  // doesn't show a stuck spinner after the chat is loaded from history.
  const endedAtIso =
    typeof payload.endedAt === "string" ? (payload.endedAt as string) : null;
  const sweptBlocks: BlockState[] = terminal
    ? blocks.map((b) =>
        b.state === "running"
          ? {
              ...b,
              state: terminal === "error" ? "failed" : "completed",
              endedAt: b.endedAt ?? endedAtIso,
            }
          : b,
      )
    : blocks;

  const priorBlockCount =
    typeof payload.priorBlockCount === "number"
      ? (payload.priorBlockCount as number)
      : null;

  return {
    ...EMPTY_NARRATIVE,
    turnId,
    turnIndex,
    mode: mode as TurnNarrativeState["mode"],
    responseType,
    cancelled: payload.cancelled === true,
    proposalDisposition,
    responseKind: parseResponseKind(payload.responseKind),
    verifiedSuccess:
      typeof payload.verifiedSuccess === "boolean"
        ? payload.verifiedSuccess
        : null,
    terminalEnvelope: parseTerminalEnvelope(payload.terminalEnvelope),
    designStarted: true,
    designEnded: true,
    draft,
    blocks: sweptBlocks,
    designActivity: normalizeActivityEntries(payload.designActivity),
    terminal,
    terminalMessage:
      typeof payload.terminalMessage === "string"
        ? (payload.terminalMessage as string)
        : null,
    narrativeSummary:
      typeof payload.narrativeSummary === "string"
        ? (payload.narrativeSummary as string)
        : null,
    priorBlockCount,
    startedAt:
      typeof payload.startedAt === "string"
        ? (payload.startedAt as string)
        : null,
    endedAt:
      typeof payload.endedAt === "string" ? (payload.endedAt as string) : null,
    credentialPrompt: parseCredentialPrompt(payload.credentialPrompt),
    credentialPause: parseCredentialPause(payload.credentialPause),
  };
}

// Chip mode should reflect what the turn ACTUALLY did, not the pre-turn
// intent classifier's guess. Re-derive from observed state so a
// classifier-said-"unknown" turn that built shows "build", and a
// classifier-said-"draft_only" turn that only asked a clarification
// shows "clarify".
export function effectiveMode(turn: TurnNarrativeState): string {
  if (turn.responseType === "ASK_QUESTION") {
    return "clarify";
  }
  const blockCount = turn.draft?.blockCount ?? turn.blocks.length;
  if (blockCount > 0) {
    const priorBlocks = turn.priorBlockCount ?? 0;
    return priorBlocks > 0 ? "edit" : "build";
  }
  if (turn.terminal !== null) {
    if (
      turn.mode === "docs_answer" ||
      turn.mode === "diagnose" ||
      turn.mode === "refuse"
    ) {
      return turn.mode;
    }
    return "clarify";
  }
  return turn.mode;
}

// History rows persisted before narrative_payload carried responseKind still
// have the adjacent persisted turn_outcome; graft its response_kind so
// adjudicated clarify/refuse/recover history corrects retroactively.
// verifiedSuccess stays null (unknown), so grafted build-kind rows render
// via the legacy inference chain and tested successes never downgrade.
export function hydrateHistoryNarrative(
  payload: Record<string, unknown> | null | undefined,
  turnOutcome: { response_kind?: string | null } | null | undefined,
): TurnNarrativeState | undefined {
  const hydrated = hydrateNarrativeFromPayload(payload);
  if (!hydrated || hydrated.responseKind !== null) return hydrated;
  const grafted = parseResponseKind(turnOutcome?.response_kind);
  if (grafted === null) return hydrated;
  return { ...hydrated, responseKind: grafted };
}

export function formatElapsed(
  startedAt: string | null,
  endedAt: string | null,
): string | null {
  const startMs = parseUtcIsoMs(startedAt);
  const endMs = parseUtcIsoMs(endedAt);
  if (startMs === null || endMs === null) return null;
  const seconds = Math.max(0, Math.round((endMs - startMs) / 1000));
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export interface TurnSummary {
  headline: string;
  stats: string[];
  accent: "ok" | "fail" | "qa" | "warn";
  glyph: string;
  isFail: boolean;
  isQA: boolean;
  isStoppedWithDraft: boolean;
}

export function latestBlocksByLabel(blocks: BlockState[]): BlockState[] {
  const latest = new Map<string, BlockState>();
  for (const block of blocks) {
    latest.set(block.label, block);
  }
  return Array.from(latest.values());
}

// Legacy prose heuristic, consulted only when the turn has no typed
// adjudication (responseKind null).
function asksUserForInput(turn: TurnNarrativeState): boolean {
  if (turn.responseType === "ASK_QUESTION") {
    return true;
  }
  const text = `${turn.terminalMessage ?? ""} ${turn.narrativeSummary ?? ""}`
    .toLowerCase()
    .trim();
  return (
    text.includes("please provide") ||
    text.includes("please share") ||
    text.includes("could you provide") ||
    text.includes("can you provide") ||
    /^(which|what|where|who|when|how)\b[\s\S]*\?/.test(text)
  );
}

interface AdjudicatedParts {
  headline: string;
  accent: TurnSummary["accent"];
  glyph: string;
}

export interface NotConfirmedOutcome {
  verdict: "not_demonstrated";
  displayReason: string | null;
}

export function notConfirmedOutcome(
  turn: Pick<
    TurnNarrativeState,
    "terminalEnvelope" | "lastRunOutcome" | "blocks"
  >,
): NotConfirmedOutcome | null {
  // The backend-finalized envelope is authoritative once it carries a run
  // verdict; the pointer/block inference below only covers rows persisted
  // before the envelope existed (or envelopes from run-less turns).
  const envelope = turn.terminalEnvelope;
  if (envelope !== null && envelope.runVerdict !== null) {
    return envelope.runVerdict === "not_demonstrated"
      ? {
          verdict: "not_demonstrated",
          displayReason: envelope.runDisplayReason,
        }
      : null;
  }
  if (turn.lastRunOutcome !== null) {
    return turn.lastRunOutcome.verdict === "not_demonstrated"
      ? {
          verdict: "not_demonstrated",
          displayReason: turn.lastRunOutcome.displayReason,
        }
      : null;
  }
  for (let i = turn.blocks.length - 1; i >= 0; i -= 1) {
    const block = turn.blocks[i]!;
    if (block.outcome === "not_demonstrated") {
      return {
        verdict: "not_demonstrated",
        displayReason: block.outcomeReason ?? null,
      };
    }
  }
  return null;
}

// Headline parts from the typed terminal adjudication. Returns null when the
// turn predates the typed signal; a build kind without a verdict also falls
// back to the legacy inference chain so genuinely-tested historical turns
// never downgrade.
function adjudicatedSummaryParts(
  turn: TurnNarrativeState,
  flags: {
    needsUntestedProposalReview: boolean;
    needsTestedProposalReview: boolean;
    hasEdited: boolean;
    hasDrafts: boolean;
    hasCleanCompletedBuild: boolean;
  },
  uxV1 = false,
): AdjudicatedParts | null {
  if (turn.responseKind === null) return null;
  // Disposition-first (rule A): a pending draft review outranks why the turn
  // ended, regardless of responseKind (including non-build/clarify turns).
  if (uxV1 && flags.needsUntestedProposalReview) {
    return { headline: "Draft needs review", accent: "qa", glyph: "!" };
  }
  if (uxV1 && flags.needsTestedProposalReview) {
    return { headline: "Workflow ready for review", accent: "qa", glyph: "!" };
  }
  if (turn.responseKind !== "build") {
    if (turn.responseKind === "refuse") {
      return { headline: "Declined", accent: "qa", glyph: "✦" };
    }
    if (turn.responseKind === "diagnose") {
      return { headline: "Answered", accent: "qa", glyph: "✦" };
    }
    if (
      turn.responseType !== "ASK_QUESTION" &&
      notConfirmedOutcome(turn)?.verdict === "not_demonstrated"
    ) {
      return { headline: "Outcome not confirmed", accent: "warn", glyph: "!" };
    }
    return {
      headline: uxV1 ? "Needs your input" : "Question",
      accent: "qa",
      glyph: "✦",
    };
  }
  if (turn.verifiedSuccess === null) return null;
  if (flags.needsUntestedProposalReview) {
    return { headline: "Draft needs review", accent: "qa", glyph: "!" };
  }
  if (flags.needsTestedProposalReview) {
    return { headline: "Workflow ready for review", accent: "qa", glyph: "!" };
  }
  if (turn.responseType === "ASK_QUESTION") {
    return {
      headline: uxV1 ? "Needs your input" : "Question",
      accent: "qa",
      glyph: "✦",
    };
  }
  if (!turn.verifiedSuccess) {
    if (flags.hasCleanCompletedBuild) {
      return {
        headline: flags.hasEdited
          ? "Applied edits and ran the workflow"
          : "Built and ran the workflow",
        accent: "ok",
        glyph: "✓",
      };
    }
    return { headline: "Stopped", accent: "qa", glyph: "!" };
  }
  if (flags.hasEdited) {
    return {
      headline: "Applied edits and re-tested",
      accent: "ok",
      glyph: "✓",
    };
  }
  if (flags.hasDrafts) {
    return {
      headline: "Built and tested the workflow",
      accent: "ok",
      glyph: "✓",
    };
  }
  return { headline: "Completed the run", accent: "ok", glyph: "✓" };
}

export function computeTurnSummary(
  turn: TurnNarrativeState,
  opts: { uxV1?: boolean } = {},
): TurnSummary {
  const uxV1 = opts.uxV1 ?? false;
  const rollupBlocks = latestBlocksByLabel(turn.blocks);
  const isFail =
    turn.terminal === "error" || rollupBlocks.some((b) => b.state === "failed");
  const mode = effectiveMode(turn);
  const needsInput = asksUserForInput(turn);
  const isQA =
    mode === "docs_answer" ||
    mode === "diagnose" ||
    mode === "clarify" ||
    mode === "refuse";
  const hasDrafts = (turn.draft?.blockCount ?? 0) > 0;
  const needsUntestedProposalReview =
    hasDrafts && turn.proposalDisposition === "review_untested";
  const needsTestedProposalReview =
    hasDrafts && turn.proposalDisposition === "review_tested";
  const hasEdited = (turn.priorBlockCount ?? 0) > 0 && hasDrafts;
  const hasCleanCompletedBuild =
    hasDrafts &&
    rollupBlocks.length > 0 &&
    rollupBlocks.every((block) => isBlockOk(block));
  const hasReviewableDraft =
    hasDrafts &&
    (turn.proposalDisposition === "review_untested" ||
      turn.proposalDisposition === "review_tested" ||
      (turn.cancelled && turn.proposalDisposition !== "no_proposal"));
  const isStoppedWithDraft = hasReviewableDraft && (isFail || turn.cancelled);

  // Fail/cancel precedence is absolute: a verdict never upgrades a halt.
  const adjudicated =
    isStoppedWithDraft || isFail
      ? null
      : adjudicatedSummaryParts(
          turn,
          {
            needsUntestedProposalReview,
            needsTestedProposalReview,
            hasEdited,
            hasDrafts,
            hasCleanCompletedBuild,
          },
          uxV1,
        );

  const headline = adjudicated
    ? adjudicated.headline
    : isStoppedWithDraft
      ? "Stopped with a draft"
      : isFail
        ? "Run halted"
        : uxV1
          ? needsUntestedProposalReview
            ? "Draft needs review"
            : needsTestedProposalReview
              ? "Workflow ready for review"
              : needsInput
                ? "Needs your input"
                : isQA
                  ? mode === "refuse"
                    ? "Declined"
                    : mode === "clarify"
                      ? "Needs your input"
                      : "Answered"
                  : hasEdited
                    ? "Applied edits and re-tested"
                    : hasDrafts
                      ? "Built and tested the workflow"
                      : "Completed the run"
          : needsInput
            ? "Question"
            : needsUntestedProposalReview
              ? "Draft needs review"
              : needsTestedProposalReview
                ? "Workflow ready for review"
                : isQA
                  ? mode === "refuse"
                    ? "Declined"
                    : mode === "clarify"
                      ? "Question"
                      : "Answered"
                  : hasEdited
                    ? "Applied edits and re-tested"
                    : hasDrafts
                      ? "Built and tested the workflow"
                      : "Completed the run";

  const stats: string[] = [];
  const turnElapsed = formatElapsed(turn.startedAt, turn.endedAt);
  if (turnElapsed) stats.push(turnElapsed);
  if (!isQA) {
    const ok = rollupBlocks.filter((b) => isBlockOk(b)).length;
    const failed = rollupBlocks.filter((b) => b.state === "failed").length;
    const newBlocks = hasEdited ? 0 : (turn.draft?.blockCount ?? 0);
    if (ok) stats.push(`${ok} block${ok === 1 ? "" : "s"} ran`);
    if (newBlocks) stats.push(`${newBlocks} new`);
    if (failed) stats.push(`${failed} failed`);
  }

  const accent = adjudicated
    ? adjudicated.accent
    : isStoppedWithDraft
      ? "qa"
      : isFail
        ? "fail"
        : needsUntestedProposalReview || needsTestedProposalReview || isQA
          ? "qa"
          : "ok";
  return {
    headline,
    stats,
    accent,
    glyph: adjudicated
      ? adjudicated.glyph
      : isStoppedWithDraft ||
          needsUntestedProposalReview ||
          needsTestedProposalReview
        ? "!"
        : isFail
          ? "✕"
          : isQA
            ? "✦"
            : "✓",
    isFail,
    isQA,
    isStoppedWithDraft,
  };
}
