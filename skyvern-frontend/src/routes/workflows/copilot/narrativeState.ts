// Pure reducer + types for the workflow copilot turn-narrative bubble. Kept
// separate from NarrativeView.tsx so Vite Fast Refresh can hot-reload the
// component without re-evaluating reducer state, and so the reducer can be
// exercised under vitest without a JSX runtime.

import { buildRevealOffsets } from "./actionReveal";
import {
  BudgetExpiryOutcome,
  ConnectedAccountChoice,
  CopilotResponseType,
  ProposalDisposition,
  RunOutcomeRole,
  WorkflowCopilotBlockProgressUpdate,
  WorkflowCopilotDesignEndUpdate,
  WorkflowCopilotDesignStartUpdate,
  WorkflowCopilotNarrationUpdate,
  WorkflowCopilotRunOutcomeUpdate,
  WorkflowCopilotStreamErrorUpdate,
  WorkflowCopilotStreamResponseUpdate,
  WorkflowCopilotToolCallUpdate,
  CodeWriteDiff,
  WorkflowCopilotToolResultUpdate,
  WorkflowCopilotTurnStartUpdate,
  WorkflowCopilotWorkflowDraftUpdate,
} from "./workflowCopilotTypes";

export interface BudgetExpiryState {
  budgetExpired: true;
  source: "deadline" | "max_turns";
  reportProduced: boolean | null;
  stagedDraftId: string | null;
  drainFingerprint: string | null;
}

function parseBudgetExpiry(raw: unknown): BudgetExpiryState | null {
  if (!raw || typeof raw !== "object") return null;
  const value = raw as Record<string, unknown>;
  if (
    value.budgetExpired !== true ||
    (value.source !== "deadline" && value.source !== "max_turns")
  ) {
    return null;
  }
  return {
    budgetExpired: true,
    source: value.source,
    reportProduced:
      typeof value.reportProduced === "boolean" ? value.reportProduced : null,
    stagedDraftId:
      typeof value.stagedDraftId === "string" ? value.stagedDraftId : null,
    drainFingerprint:
      typeof value.drainFingerprint === "string"
        ? value.drainFingerprint
        : null,
  };
}

function budgetExpiryFromOutcome(
  outcome: BudgetExpiryOutcome | null | undefined,
): BudgetExpiryState | null {
  if (
    outcome?.budget_expired !== true ||
    (outcome.budget_expiry_source !== "deadline" &&
      outcome.budget_expiry_source !== "max_turns")
  ) {
    return null;
  }
  return {
    budgetExpired: true,
    source: outcome.budget_expiry_source,
    reportProduced: outcome.budget_expiry_report_produced ?? null,
    stagedDraftId: outcome.budget_expiry_staged_draft_id ?? null,
    drainFingerprint: outcome.drain_fingerprint ?? null,
  };
}

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
// failed-style states (failed, terminated, timed_out) under one chip and
// treats `skipped` and `stopped` as separate neutral states. `stopped` is a
// user cancel: it ran and was interrupted, which is not a failure and must
// never be rendered as one.
export type BlockUIState =
  | "queued"
  | "drafted"
  | "running"
  | "completed"
  | "failed"
  | "stopped"
  | "skipped";

// Recorded per-run outcome verdict, distinct from lifecycle state: a row can
// be `completed` (it ran) yet carry a `not_demonstrated` verdict.
export type BlockOutcome =
  | "evaluating"
  | "demonstrated"
  | "not_demonstrated"
  | "not_evaluated";

export function isInterimOutcome(role: RunOutcomeRole | undefined): boolean {
  return role === "interim_build_test";
}

// The completion judge's verdict text is a backend log/policy contract, so it is rewritten
// at the display layer rather than at the source. The backend also appends it to the
// assistant's closing message, so both paths run through this. Unrecognized text passes through.
const JUDGE_REWRITES: ReadonlyArray<readonly [RegExp, string]> = [
  [
    /The run completed but did not demonstrate the goal outcome\(s\)\.\s*Missing evidence:\s*/g,
    "The run finished but didn't produce what you asked for: ",
  ],
  [
    /The run completed but did not demonstrate the goal outcome\(s\):\s*/g,
    "The run finished but didn't produce what you asked for: ",
  ],
  [
    /The run completed but did not demonstrate the goal outcome\(s\)\./g,
    "The run finished but didn't produce what you asked for.",
  ],
];

// Repair instruction aimed at the agent; the user is not the one editing blocks. The backend
// truncates display_reason to 160 chars, so it usually arrives cut at an arbitrary point.
const JUDGE_INSTRUCTION =
  "Add or fix the block that produces the missing outcome evidence, then re-run.";

// Only ever reached for text whose judge headline was recognized. The trailing-prefix scan
// would otherwise eat the last word of ordinary prose ("Choose option A" -> "Choose option").
function stripJudgeInstruction(text: string): string {
  const found = text.indexOf(JUDGE_INSTRUCTION);
  if (found !== -1) {
    return text.slice(0, found) + text.slice(found + JUDGE_INSTRUCTION.length);
  }
  // The 160-char budget cut the instruction short, so it always runs to end of string.
  for (let i = 1; i < text.length; i += 1) {
    if (!/\s/.test(text[i - 1]!)) continue;
    if (JUDGE_INSTRUCTION.startsWith(text.slice(i))) {
      return text.slice(0, i);
    }
  }
  return text;
}

export function humanizeJudgeText(text: string): string {
  const rewritten = JUDGE_REWRITES.reduce(
    (result, [pattern, replacement]) => result.replace(pattern, replacement),
    text,
  );
  // Free assistant prose never carries the judge headline, so leave it exactly as written.
  if (rewritten === text) {
    return text;
  }
  return stripJudgeInstruction(rewritten).replace(/ {2,}/g, " ").trim();
}

export function terminalNarrativeText(turn: TurnNarrativeState): string {
  if (turn.budgetExpiry?.reportProduced === false) {
    const limit =
      turn.budgetExpiry.source === "deadline" ? "time" : "model-call";
    const status = `This turn reached its ${limit} limit without producing a report.`;
    return turn.budgetExpiry.stagedDraftId
      ? `${status} A draft was staged during this session.`
      : status;
  }
  if (turn.budgetExpiry) {
    const drainReply = turn.terminalMessage?.trim() || "";
    if (drainReply) return drainReply;
  }
  return turn.narrativeSummary?.trim() || turn.terminalMessage?.trim() || "";
}

type BuildTestConnectFailureState =
  | "already_closed"
  | "provisioning_unavailable"
  | "cdp_connect_failed"
  | "occupied";

function isBuildTestConnectFailureState(
  value: unknown,
): value is BuildTestConnectFailureState {
  return (
    value === "already_closed" ||
    value === "provisioning_unavailable" ||
    value === "cdp_connect_failed" ||
    value === "occupied"
  );
}

// Closed vocabulary of TerminalOutcomeEnvelope.next_state. The envelope's
// sibling response_kind and user_action_required carry the same bit for a
// question turn, so only this one is parsed.
export type TerminalNextState =
  | "completed"
  | "proposal_pending"
  | "awaiting_user_input"
  | "stopped";

export interface TerminalEnvelopeFacts {
  nextState: TerminalNextState | null;
  // The backend stamps this only when the terminal-envelope render flag is on
  // for the org; until then the envelope is carried but is not display
  // authority, so no surface may key off it.
  renderedFromEnvelope: boolean;
  runVerdict: BlockOutcome | null;
  runDisplayReason: string | null;
  // Absent on envelopes persisted before the run id was carried.
  runId?: string | null;
  // Absent on envelopes persisted before the role was carried.
  runOutcomeRole?: RunOutcomeRole | null;
  connectFailure?: {
    state: BuildTestConnectFailureState;
    retryAction: "test_end_to_end";
    workflowRunId: string | null;
    workflowRunBlockId: string | null;
    taskId: string | null;
    browserSessionId: string | null;
  } | null;
}

export type ReviewChange = "added" | "changed" | "unchanged" | "removed";
export type BlockCoverage =
  | "current_source"
  | "different_source"
  | "never_run"
  | "unknown";

const BLOCK_COVERAGE: readonly BlockCoverage[] = [
  "current_source",
  "different_source",
  "never_run",
  "unknown",
];

// The canonical per-turn facts the backend publishes; card, pill and prose each
// render a projection of this bundle rather than deriving their own verdict.
export interface TurnFacts {
  factsAvailable: boolean;
  authoredBlockCount: number | null;
  matchingSourceBlockCount: number | null;
  evaluationState: BlockOutcome | null;
  runId: string | null;
  runCompleted: boolean | null;
  terminalCause: string | null;
  blocksRunThisTurn: number | null;
  ranCleanOnCurrentSource: boolean;
}

// The backend decides the tested claim and publishes it here; no surface recomputes it.
export function ranCleanOnCurrentSource(facts: TurnFacts | null): boolean {
  return facts?.ranCleanOnCurrentSource === true;
}

export interface ReviewProjection {
  blocks: Array<{
    label: string;
    blockType: string;
    change: ReviewChange;
    neverTested?: boolean;
    coverage?: BlockCoverage;
  }>;
  duplicateWrites: Array<{
    blockType: string;
    blockLabels: string[];
  }>;
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
  const rawConnectFailure = obj.connect_failure;
  const connectFailure = (() => {
    if (!rawConnectFailure || typeof rawConnectFailure !== "object")
      return null;
    const failure = rawConnectFailure as Record<string, unknown>;
    const state = failure.state;
    if (
      !isBuildTestConnectFailureState(state) ||
      failure.retry_action !== "test_end_to_end"
    ) {
      return null;
    }
    const text = (key: string) =>
      typeof failure[key] === "string" ? (failure[key] as string) : null;
    return {
      state,
      retryAction: "test_end_to_end" as const,
      workflowRunId: text("workflow_run_id"),
      workflowRunBlockId: text("workflow_run_block_id"),
      taskId: text("task_id"),
      browserSessionId: text("browser_session_id"),
    };
  })();
  const role = obj.run_outcome_role;
  const nextState = obj.next_state;
  return {
    nextState:
      nextState === "completed" ||
      nextState === "proposal_pending" ||
      nextState === "awaiting_user_input" ||
      nextState === "stopped"
        ? nextState
        : null,
    renderedFromEnvelope: obj.rendered_from_envelope === true,
    runVerdict:
      v === "demonstrated" || v === "not_demonstrated" || v === "not_evaluated"
        ? v
        : null,
    runDisplayReason:
      typeof obj.run_display_reason === "string"
        ? obj.run_display_reason
        : null,
    runId: typeof obj.run_id === "string" ? obj.run_id : null,
    runOutcomeRole:
      role === "recorded" ||
      role === "adjudicated" ||
      role === "interim_build_test"
        ? role
        : null,
    connectFailure,
  };
}

// The one typed source for "this turn ended on the user". The backend derives
// next_state from response_type == ASK_QUESTION and keeps it through a failed
// build test, so a question asked after scouting or a run still reads as one.
// Read-gated: the envelope is display authority only once the backend stamped
// rendered_from_envelope. A user stop is not an ask -- next_state is derived
// before the cancel lands, so the cancel path persists awaiting_user_input on a
// turn nobody is waiting on.
export function awaitsUserInput(
  turn:
    | Pick<TurnNarrativeState, "terminalEnvelope" | "cancelled">
    | null
    | undefined,
): boolean {
  if (!turn || turn.cancelled) return false;
  const envelope = turn.terminalEnvelope;
  return (
    envelope?.renderedFromEnvelope === true &&
    envelope.nextState === "awaiting_user_input"
  );
}

export interface BlockState {
  workflowRunBlockId: string;
  label: string;
  blockType: string;
  state: BlockUIState;
  // Undefined when no verdict was recorded (older backend or unadjudicated run).
  outcome?: BlockOutcome;
  outcomeReason?: string;
  outcomeRole?: RunOutcomeRole;
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
  // activeLabel reads while the step runs; outcomeLabel replaces it once
  // finished. Absent when the narrator did not speak for this step.
  activeLabel?: string;
  outcomeLabel?: string;
  // Result success when kind is tool_result.
  success?: boolean;
  // Server-computed line delta per code block this write changed. Absent on
  // every other row and on payloads from a backend that predates it.
  codeDiffs?: CodeWriteDiff[];
  // Stable per-event id used as React key.
  id: string;
  // Consecutive same-tool retries folded into this row by
  // condenseActivityEntries. Unset outside that transform.
  attempts?: number;
  // First-attempt identity for the reader-visible retry row. The current
  // entry keeps its real event id for correlation.
  retryRootId?: string;
  // Exact failed attempts hidden by retry condensation, retained for the
  // expanded evidence view.
  priorFailures?: ActivityEntry[];
  // Server timestamp of the first call represented by this condensed entry.
  // A settled tool_result otherwise retains only its completion timestamp,
  // which makes real multi-second work render as 0:00.
  activityStartedAt?: string;
  // Server clock read for this event, persisted so a hydrated turn reports the
  // same elapsed as the live one. Undefined against a backend that does not stamp.
  timestamp?: string;
  // Epoch ms this entry arrived on the live stream. Absent on hydrate, which
  // is what makes a reloaded narration render complete instead of replaying.
  receivedAtMs?: number;
}

// Closed vocabulary of the backend TurnOutcome.response_kind enum. Unknown
// wire values parse to null so a newer backend cannot crash the renderer.
export type TurnResponseKind =
  | "answer"
  | "build"
  | "clarify"
  | "diagnose"
  | "refuse"
  | "recover";

export interface TurnNarrativeState {
  turnId: string | null;
  turnIndex: number | null;
  responseType: CopilotResponseType | null;
  proposalDisposition: ProposalDisposition | null;
  // Typed terminal response kind for the turn (TurnOutcome.response_kind).
  // Null on legacy rows and frames from an older backend.
  responseKind: TurnResponseKind | null;
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
  // so it survives the MAX_DESIGN_ACTIVITY_ENTRIES eviction cap.
  authoringCount: number;
  // Snapshot of the most recent factual run outcome.
  lastRunOutcome: {
    verdict: BlockOutcome;
    role?: RunOutcomeRole;
    displayReason: string | null;
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
  // Silently auto-bound credential, from the credentialAutoBound narrative signal — rendered as a
  // receipt with a Change affordance so a confident-but-wrong pick can be corrected after the fact.
  credentialAutoBound: { credentialId: string; name: string } | null;
  connectedAccountChoices: ConnectedAccountChoice[];
  googleConnectionNotices: GoogleConnectionNotice[];
  review: ReviewProjection | null;
  turnFacts: TurnFacts | null;
  budgetExpiry: BudgetExpiryState | null;
}

export interface GoogleConnectionNotice {
  provider: "google";
  connectionId: string | null;
  displayName: string | null;
  condition: "missing" | "unusable" | "unbound";
  choices?: ConnectedAccountChoice[];
}

export const EMPTY_NARRATIVE: TurnNarrativeState = Object.freeze({
  turnId: null,
  turnIndex: null,
  responseType: null,
  proposalDisposition: null,
  responseKind: null,
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
  lastRunOutcome: null,
  credentialPrompt: null,
  credentialPause: null,
  credentialAutoBound: null,
  connectedAccountChoices: [],
  googleConnectionNotices: [],
  review: null,
  turnFacts: null,
  budgetExpiry: null,
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
  return value === "answer" ||
    value === "build" ||
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

export function parseCodeDiffs(value: unknown): CodeWriteDiff[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const diffs: CodeWriteDiff[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    if (
      typeof row.label !== "string" ||
      typeof row.added !== "number" ||
      typeof row.removed !== "number"
    ) {
      continue;
    }
    diffs.push({
      label: row.label,
      added: row.added,
      removed: row.removed,
      patch: typeof row.patch === "string" ? row.patch : undefined,
      patchDropped: row.patchDropped === true,
    });
  }
  return diffs.length > 0 ? diffs : undefined;
}

export function parseConnectedAccountChoices(
  value: unknown,
): ConnectedAccountChoice[] {
  if (!Array.isArray(value)) return [];
  const choices: ConnectedAccountChoice[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    if (
      typeof row.connection_id !== "string" ||
      typeof row.name !== "string" ||
      typeof row.state !== "string"
    ) {
      continue;
    }
    choices.push({
      connection_id: row.connection_id,
      name: row.name,
      state: row.state,
      email_address:
        typeof row.email_address === "string" ? row.email_address : null,
    });
  }
  return choices;
}

export function parseCredentialAutoBound(
  value: unknown,
): TurnNarrativeState["credentialAutoBound"] {
  if (!value || typeof value !== "object") return null;
  const o = value as Record<string, unknown>;
  const credentialId = o.credentialId;
  const name = o.name;
  return typeof credentialId === "string" &&
    credentialId.length > 0 &&
    typeof name === "string" &&
    name.length > 0
    ? { credentialId, name }
    : null;
}

export function parseGoogleConnectionNotices(
  value: unknown,
): GoogleConnectionNotice[] {
  if (!Array.isArray(value)) return [];
  const notices: GoogleConnectionNotice[] = [];
  const seen = new Set<string | null>();
  for (const item of value) {
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    if (
      row.provider !== "google" ||
      (row.condition === "unbound"
        ? row.connectionId !== null
        : typeof row.connectionId !== "string" ||
          row.connectionId.length === 0) ||
      (row.condition !== "missing" &&
        row.condition !== "unusable" &&
        row.condition !== "unbound")
    ) {
      continue;
    }
    const connectionId =
      row.condition === "unbound" ? null : (row.connectionId as string);
    if (seen.has(connectionId)) continue;
    seen.add(connectionId);
    notices.push({
      provider: "google",
      connectionId,
      displayName:
        typeof row.displayName === "string" && row.displayName.length > 0
          ? row.displayName
          : null,
      condition: row.condition,
      ...(row.condition === "unbound"
        ? { choices: parseConnectedAccountChoices(row.choices) }
        : {}),
    });
  }
  return notices;
}

// Tool calls that write the workflow definition. update_workflow only
// validates/saves the draft; update_and_run_blocks also runs it, so it's
// the one AUTHORING_TOOLS member that's also a RUN_TOOLS member (its
// activity lands in the Test phase bucket, not Draft — see copilotPhases.ts).
export const AUTHORING_TOOLS = new Set([
  "update_workflow",
  "update_and_run_blocks",
  "edit_block_and_run",
]);
export const RUN_TOOLS = new Set([
  "update_and_run_blocks",
  "edit_block_and_run",
  "run_blocks_and_collect_debug",
]);

// Tool names we never surface in the user-facing activity log. Internal
// observation/maintenance tools are not interesting context for the user.
const ACTIVITY_TOOL_DENYLIST = new Set([
  "get_run_results",
  "get_browser_screenshot",
]);

// Version-skew fallback for the server-authored display_label: mirror of the
// backend _TOOL_ACTIVITY_DISPLAY_LABELS in narration.py.
const ACTIVITY_TOOL_DISPLAY_LABELS: Record<string, string> = {
  update_workflow: "Updating workflow",
  update_and_run_blocks: "Testing workflow",
  edit_block_and_run: "Editing and testing block",
  run_blocks_and_collect_debug: "Testing workflow",
  evaluate: "Inspecting page",
  click: "Interacting with page",
  type_text: "Entering text",
  scroll: "Interacting with page",
  select_option: "Selecting option",
  press_key: "Interacting with page",
  navigate_browser: "Opening page",
  wait_for_either_state: "Waiting for the page",
  skyvern_frame_list: "Finding embedded pages",
  skyvern_frame_switch: "Opening embedded page",
  skyvern_frame_main: "Returning to main page",
  get_block_schema: "Checking workflow block options",
  inspect_current_workflow: "Inspecting workflow",
  discover_workflow_entrypoint: "Finding the entry page",
  inspect_page_for_composition: "Inspecting the page",
  list_credentials: "Checking saved credentials",
  fill_credential_field: "Entering saved credentials",
  edit_block: "Editing block",
  add_block: "Adding block",
  delete_block: "Deleting block",
  request_credential: "Requesting a credential",
  ask_user: "Asking you",
  synthesize_demonstrated_block: "Building a block from the recorded steps",
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
    timestamp: event.timestamp ?? undefined,
  };
}

// summarize_tool_result's pre-SKY-13203 credential summary: "Found N credential(s)".
const LEGACY_CREDENTIAL_COUNT_SUMMARY_RE = /^Found \d+ credential\(s\)$/;

function buildActivityFromToolResult(
  event: WorkflowCopilotToolResultUpdate,
): ActivityEntry | null {
  if (ACTIVITY_TOOL_DENYLIST.has(event.tool_name)) {
    return null;
  }
  const displayLabel =
    event.display_label ?? toolActivityDisplayLabel(event.tool_name);
  // Drop only the legacy enumeration count a pre-SKY-13203 backend still emits
  // for credential lookups; a blocker explanation also arrives as a successful
  // summary and must survive.
  const suppressSummary =
    event.tool_name === "list_credentials" &&
    LEGACY_CREDENTIAL_COUNT_SUMMARY_RE.test(event.summary ?? "");
  return {
    kind: "tool_result",
    text: suppressSummary ? displayLabel : event.summary || displayLabel,
    iteration: event.iteration,
    toolName: event.tool_name,
    displayLabel,
    success: event.success,
    codeDiffs: parseCodeDiffs(event.code_diffs),
    id: `tr-${event.tool_call_id}`,
    timestamp: event.timestamp ?? undefined,
  };
}

function buildActivityFromNarration(
  event: WorkflowCopilotNarrationUpdate,
  receivedAtMs: number,
): ActivityEntry {
  return {
    kind: "narration",
    text: event.narration,
    iteration: event.iteration,
    activeLabel: event.active_label ?? undefined,
    outcomeLabel: event.outcome_label ?? undefined,
    id: `n-${event.iteration}-${event.timestamp}`,
    timestamp: event.timestamp,
    receivedAtMs,
  };
}

// Shared "tc-<tool_call_id>" / "tr-<tool_call_id>" id-parsing convention.
export function toolCallIdOf(entry: ActivityEntry): string | undefined {
  return entry.kind === "tool_call" || entry.kind === "tool_result"
    ? entry.id.slice(3)
    : undefined;
}

// A tool_call still has no matching tool_result — the narrator can emit a
// TOOL_STARTED progress narration mid-flight (streaming_adapter.py), so
// checking only the LAST entry's kind isn't enough; match ids instead.
export function hasPendingToolCall(designActivity: ActivityEntry[]): boolean {
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
// Correlation uses toolName plus displayLabel, which narrows the old
// toolName-only rule: edits of two differently-named blocks no longer fold.
// It is not full target identity — the label is humanized and length-capped,
// so `login_v1`/`login_v2` and two labels sharing a 40-char prefix still
// collide and fold. Raw-argument identity on the wire would close that.
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
        const call = paired[idx];
        const settled =
          call?.timestamp === undefined
            ? entry
            : {
                ...entry,
                activityStartedAt: call.activityStartedAt ?? call.timestamp,
              };
        if (idx === paired.length - 1) {
          paired[idx] = settled;
        } else {
          paired[idx] = null;
          paired.push(settled);
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
    const previousEndedMs = parseUtcIsoMs(prevTool?.timestamp);
    const currentStartedMs = parseUtcIsoMs(
      entry.activityStartedAt ?? entry.timestamp,
    );
    const followsPreviousAttempt =
      previousEndedMs === null ||
      currentStartedMs === null ||
      currentStartedMs >= previousEndedMs;
    if (
      prevTool &&
      entry.toolName !== undefined &&
      prevTool.toolName === entry.toolName &&
      (prevTool.displayLabel === undefined ||
        entry.displayLabel === undefined ||
        prevTool.displayLabel === entry.displayLabel) &&
      prevTool.success === false &&
      followsPreviousAttempt
    ) {
      const { priorFailures: earlierFailures, ...previousAttempt } = prevTool;
      condensed[lastToolIdx] = null;
      lastToolIdx =
        condensed.push({
          ...entry,
          retryRootId: prevTool.retryRootId ?? prevTool.id,
          priorFailures: [
            ...(earlierFailures ?? []),
            previousAttempt as ActivityEntry,
          ],
          attempts: (prevTool.attempts ?? 1) + 1,
          activityStartedAt:
            prevTool.activityStartedAt ??
            prevTool.timestamp ??
            entry.activityStartedAt,
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

function findActivityBlockIndex(blocks: BlockState[], entryId: string): number {
  return blocks.findIndex((block) =>
    block.activity.some((entry) => entry.id === entryId),
  );
}

function appendActivity(
  blocks: BlockState[],
  designActivity: ActivityEntry[],
  entry: ActivityEntry,
): { blocks: BlockState[]; designActivity: ActivityEntry[] } {
  // Mirrors NarratorState._activity_bucket_label: narration renders inside the
  // design step it explains, so it stays in design activity even mid-run.
  if (entry.kind === "narration") {
    return {
      blocks,
      designActivity: appendCapped(
        designActivity,
        entry,
        MAX_DESIGN_ACTIVITY_ENTRIES,
      ),
    };
  }
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
    const callBlockIdx = findActivityBlockIndex(blocks, callId);
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

function attachCodeDiffsToActivity(
  blocks: BlockState[],
  designActivity: ActivityEntry[],
  targetId: string,
  codeDiffs: CodeWriteDiff[],
): { blocks: BlockState[]; designActivity: ActivityEntry[] } {
  const designIndex = designActivity.findIndex(
    (entry) => entry.id === targetId,
  );
  if (designIndex !== -1) {
    const nextDesignActivity = designActivity.slice();
    nextDesignActivity[designIndex] = {
      ...nextDesignActivity[designIndex]!,
      codeDiffs,
    };
    return { blocks, designActivity: nextDesignActivity };
  }

  const blockIndex = findActivityBlockIndex(blocks, targetId);
  if (blockIndex === -1) return { blocks, designActivity };

  const nextBlocks = blocks.slice();
  const block = nextBlocks[blockIndex]!;
  const activityIndex = block.activity.findIndex(
    (entry) => entry.id === targetId,
  );
  const activity = block.activity.slice();
  activity[activityIndex] = { ...activity[activityIndex]!, codeDiffs };
  nextBlocks[blockIndex] = { ...block, activity };
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
      return "failed";
    case "canceled":
      return "stopped";
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

// Labels that occur exactly once in the given set — the only labels safe to key
// recorded actions on when the run-block id is missing (a block that never
// reported progress, or an older backend). Loop iterations reuse a label, so an
// ambiguous label falls back to today's drop rather than mis-attributing.
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
      // The write's patch arrives here rather than on the tool_result, because a write and its
      // test share one tool call and that result lands only after the run. Attach it to the
      // call's own entry so the row shows the code as soon as it is written.
      const draftDiffs = parseCodeDiffs(event.code_diffs);
      const diffTarget =
        event.tool_call_id == null ? null : `tc-${event.tool_call_id}`;
      const withDiffs =
        draftDiffs === undefined || diffTarget === null
          ? { blocks: nextBlocks, designActivity: prev.designActivity }
          : attachCodeDiffsToActivity(
              nextBlocks,
              prev.designActivity,
              diffTarget,
              draftDiffs,
            );

      return {
        ...prev,
        draft: {
          blockCount: event.block_count,
          blockLabels: event.block_labels,
          summary: event.summary,
        },
        blocks: withDiffs.blocks,
        designActivity: withDiffs.designActivity,
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
        incomingState === "stopped" ||
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
        outcomeRole: previousBlock?.outcomeRole,
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
              outcomeRole: event.role ?? "recorded",
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
          role: event.role ?? "recorded",
          displayReason: event.display_reason ?? null,
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
      if (!entry) {
        return {
          ...prev,
          lastActivityAtMs: nowMs,
          authoringCount,
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
      };
    }

    case "tool_result": {
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
      const entry = buildActivityFromNarration(event, nowMs);
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
        // The BE narrative_payload carries no client-only recordedActions, and
        // omits workflowRunBlockId for a block that never reported progress.
        // Re-associating by label carries both across, but only for a label that
        // occurs once — loop iterations reuse one and can't be told apart.
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
          // doesn't visually un-check the Draft phase (hydrated payloads never
          // carry these client-only fields). authoringCount is grafted too so a
          // turn whose only authoring entry aged out of the capped activity
          // list still completes Explore at the swap. lastRunOutcome remains
          // sourced from the hydrated terminal payload.
          draftingSignaledAt:
            hydrated.turnId === prev.turnId ? prev.draftingSignaledAt : null,
          authoringCount:
            hydrated.turnId === prev.turnId
              ? prev.authoringCount
              : hydrated.authoringCount,
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
      activeLabel:
        typeof o.activeLabel === "string" ? o.activeLabel : undefined,
      outcomeLabel:
        typeof o.outcomeLabel === "string" ? o.outcomeLabel : undefined,
      success: typeof o.success === "boolean" ? o.success : undefined,
      codeDiffs: parseCodeDiffs(o.codeDiffs),
      id: o.id,
      timestamp: typeof o.timestamp === "string" ? o.timestamp : undefined,
    });
  }
  return out;
}

function parseTurnFacts(raw: unknown): TurnFacts | null {
  if (!raw || typeof raw !== "object") return null;
  const value = raw as Record<string, unknown>;
  if (typeof value.factsAvailable !== "boolean") return null;
  const count = (key: string): number | null =>
    typeof value[key] === "number" ? (value[key] as number) : null;
  const text = (key: string): string | null =>
    typeof value[key] === "string" ? (value[key] as string) : null;
  const evaluationState = value.evaluationState;
  return {
    factsAvailable: value.factsAvailable,
    authoredBlockCount: count("authoredBlockCount"),
    matchingSourceBlockCount: count("matchingSourceBlockCount"),
    evaluationState:
      evaluationState === "demonstrated" ||
      evaluationState === "not_demonstrated" ||
      evaluationState === "not_evaluated"
        ? evaluationState
        : null,
    runId: text("runId"),
    runCompleted:
      typeof value.runCompleted === "boolean" ? value.runCompleted : null,
    terminalCause: text("terminalCause"),
    blocksRunThisTurn: count("blocksRunThisTurn"),
    ranCleanOnCurrentSource: value.ranCleanOnCurrentSource === true,
  };
}

function parseReviewProjection(raw: unknown): ReviewProjection | null {
  if (!raw || typeof raw !== "object") return null;
  const value = raw as Record<string, unknown>;
  if (!Array.isArray(value.blocks) || !Array.isArray(value.duplicateWrites)) {
    return null;
  }
  const blocks: ReviewProjection["blocks"] = [];
  for (const rawBlock of value.blocks) {
    if (!rawBlock || typeof rawBlock !== "object") return null;
    const block = rawBlock as Record<string, unknown>;
    if (
      typeof block.label !== "string" ||
      typeof block.blockType !== "string" ||
      (block.change !== "added" &&
        block.change !== "changed" &&
        block.change !== "unchanged" &&
        block.change !== "removed") ||
      (block.neverTested !== undefined &&
        typeof block.neverTested !== "boolean")
    ) {
      return null;
    }
    // An unrecognised coverage value drops off this block rather than voiding the
    // whole projection, so a newer backend vocabulary cannot blank the review.
    blocks.push({
      label: block.label,
      blockType: block.blockType,
      change: block.change,
      neverTested: block.neverTested,
      coverage: BLOCK_COVERAGE.find((known) => known === block.coverage),
    });
  }
  const duplicateWrites: ReviewProjection["duplicateWrites"] = [];
  for (const rawGroup of value.duplicateWrites) {
    if (!rawGroup || typeof rawGroup !== "object") return null;
    const group = rawGroup as Record<string, unknown>;
    if (
      typeof group.blockType !== "string" ||
      !Array.isArray(group.blockLabels) ||
      !group.blockLabels.every((label) => typeof label === "string")
    ) {
      return null;
    }
    duplicateWrites.push({
      blockType: group.blockType,
      blockLabels: group.blockLabels,
    });
  }
  return { blocks, duplicateWrites };
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
    const outcomeRole: RunOutcomeRole | undefined =
      outcome === undefined
        ? undefined
        : obj.outcomeRole === "recorded" ||
            obj.outcomeRole === "adjudicated" ||
            obj.outcomeRole === "interim_build_test"
          ? obj.outcomeRole
          : "adjudicated";
    return {
      workflowRunBlockId:
        typeof obj.workflowRunBlockId === "string"
          ? obj.workflowRunBlockId
          : "",
      label: typeof obj.label === "string" ? obj.label : "",
      blockType: typeof obj.blockType === "string" ? obj.blockType : "task",
      outcome,
      outcomeRole,
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
          s === "stopped" ||
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
  const cancelled = payload.cancelled === true;
  const turnFacts = parseTurnFacts(payload.turnFacts);
  // A deadline cuts a running block off; that is a stop, so it must not be swept
  // to failed and then read back as the turn having failed.
  const budgetExpiry = parseBudgetExpiry(payload.budgetExpiry);
  const halted =
    cancelled ||
    turnFacts?.terminalCause === "deadline_expired" ||
    budgetExpiry !== null;
  const sweptBlocks: BlockState[] = terminal
    ? blocks.map((b) =>
        b.state === "running"
          ? {
              ...b,
              state: halted
                ? "stopped"
                : terminal === "error"
                  ? "failed"
                  : "completed",
              endedAt: b.endedAt ?? endedAtIso,
            }
          : b,
      )
    : blocks;

  // The backend stamps a canceled block "failed" and a canceled turn's terminal
  // "error" (`_BLOCK_STATUS_TO_UI_STATE`, agent.py), so the settle frame and
  // every reload would repaint a user's own stop as a failure. `cancelled` is
  // the turn-level truth the same payload already carries.
  const stoppedBlocks: BlockState[] = cancelled
    ? sweptBlocks.map((b) =>
        b.state === "failed" ? { ...b, state: "stopped" as BlockUIState } : b,
      )
    : sweptBlocks;

  const priorBlockCount =
    typeof payload.priorBlockCount === "number"
      ? (payload.priorBlockCount as number)
      : null;

  return {
    ...EMPTY_NARRATIVE,
    turnId,
    turnIndex,
    responseType,
    cancelled,
    proposalDisposition,
    responseKind: parseResponseKind(payload.responseKind),
    terminalEnvelope: parseTerminalEnvelope(payload.terminalEnvelope),
    designStarted: true,
    designEnded: true,
    draft,
    blocks: stoppedBlocks,
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
    credentialAutoBound: parseCredentialAutoBound(payload.credentialAutoBound),
    connectedAccountChoices: parseConnectedAccountChoices(
      payload.connectedAccountChoices,
    ),
    googleConnectionNotices: parseGoogleConnectionNotices(
      payload.googleConnectionNotices,
    ),
    review: parseReviewProjection(payload.review),
    turnFacts,
    budgetExpiry,
  };
}

// History rows persisted before narrative_payload carried responseKind still
// have the adjacent persisted turn_outcome; graft its response_kind so
// clarify/refuse/recover history corrects retroactively. Build-kind rows use
// lifecycle and run facts, so grafting a response kind cannot change them.
export function hydrateHistoryNarrative(
  payload: Record<string, unknown> | null | undefined,
  turnOutcome:
    | (BudgetExpiryOutcome & {
        response_kind?: string | null;
        connected_account_choices?: ConnectedAccountChoice[] | null;
      })
    | null
    | undefined,
): TurnNarrativeState | undefined {
  const hydrated = hydrateNarrativeFromPayload(payload);
  if (!hydrated) return hydrated;
  const grafted = parseResponseKind(turnOutcome?.response_kind);
  const choices = parseConnectedAccountChoices(
    turnOutcome?.connected_account_choices,
  );
  const hasTurnOutcomeChoices =
    turnOutcome !== null &&
    turnOutcome !== undefined &&
    Object.prototype.hasOwnProperty.call(
      turnOutcome,
      "connected_account_choices",
    );
  return {
    ...hydrated,
    responseKind: hydrated.responseKind ?? grafted,
    connectedAccountChoices: hasTurnOutcomeChoices
      ? choices
      : hydrated.connectedAccountChoices,
    budgetExpiry: hydrated.budgetExpiry ?? budgetExpiryFromOutcome(turnOutcome),
  };
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

export function latestBlocksByLabel(blocks: BlockState[]): BlockState[] {
  const latest = new Map<string, BlockState>();
  for (const block of blocks) {
    latest.set(block.label, block);
  }
  return Array.from(latest.values());
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
  if (
    envelope !== null &&
    envelope.runVerdict !== null &&
    !isInterimOutcome(envelope.runOutcomeRole ?? undefined)
  ) {
    return envelope.runVerdict === "not_demonstrated"
      ? {
          verdict: "not_demonstrated",
          displayReason: envelope.runDisplayReason,
        }
      : null;
  }
  if (turn.lastRunOutcome !== null) {
    return turn.lastRunOutcome.verdict === "not_demonstrated" &&
      !isInterimOutcome(turn.lastRunOutcome.role)
      ? {
          verdict: "not_demonstrated",
          displayReason: turn.lastRunOutcome.displayReason,
        }
      : null;
  }
  for (let i = turn.blocks.length - 1; i >= 0; i -= 1) {
    const block = turn.blocks[i]!;
    if (
      block.outcome === "not_demonstrated" &&
      !isInterimOutcome(block.outcomeRole)
    ) {
      return {
        verdict: "not_demonstrated",
        displayReason: block.outcomeReason ?? null,
      };
    }
  }
  return null;
}

// A deadline exit stamps terminal="error" for budget reasons; that is a halt,
// not a block failure, so no surface may give it failure's treatment.
export function isDeadlineHalt(turn: TurnNarrativeState): boolean {
  return (
    (turn.turnFacts?.terminalCause === "deadline_expired" ||
      turn.budgetExpiry !== null) &&
    !latestBlocksByLabel(turn.blocks).some((b) => b.state === "failed")
  );
}
