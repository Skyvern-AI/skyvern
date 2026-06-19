// Pure reducer + types for the workflow copilot turn-narrative bubble. Kept
// separate from NarrativeView.tsx so Vite Fast Refresh can hot-reload the
// component without re-evaluating reducer state, and so the reducer can be
// exercised under vitest without a JSX runtime.

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
  | WorkflowCopilotToolResultUpdate;

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
}

export const EMPTY_NARRATIVE: TurnNarrativeState = Object.freeze({
  turnId: null,
  turnIndex: null,
  mode: "unknown",
  responseType: null,
  proposalDisposition: null,
  responseKind: null,
  verifiedSuccess: null,
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

function appendCapped<T>(arr: T[], entry: T, cap: number): T[] {
  const next = [...arr, entry];
  return next.length > cap ? next.slice(next.length - cap) : next;
}

function appendActivity(
  blocks: BlockState[],
  designActivity: ActivityEntry[],
  entry: ActivityEntry,
): { blocks: BlockState[]; designActivity: ActivityEntry[] } {
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

export function applyNarrativeEvent(
  prev: TurnNarrativeState,
  event: NarrativeEvent,
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
        // A late or replayed block_progress must not wipe a recorded verdict;
        // lifecycle frames never carry outcome, so always keep the prior one.
        outcome: previousBlock?.outcome,
        outcomeReason: previousBlock?.outcomeReason,
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
      return { ...prev, blocks };
    }

    case "tool_call": {
      const entry = buildActivityFromToolCall(event);
      if (!entry) return prev;
      const { blocks, designActivity } = appendActivity(
        prev.blocks,
        prev.designActivity,
        entry,
      );
      return { ...prev, blocks, designActivity };
    }

    case "tool_result": {
      const entry = buildActivityFromToolResult(event);
      if (!entry) return prev;
      const { blocks, designActivity } = appendActivity(
        prev.blocks,
        prev.designActivity,
        entry,
      );
      return { ...prev, blocks, designActivity };
    }

    case "narration": {
      const entry = buildActivityFromNarration(event);
      const { blocks, designActivity } = appendActivity(
        prev.blocks,
        prev.designActivity,
        entry,
      );
      return { ...prev, blocks, designActivity };
    }

    case "response": {
      const hydrated = hydrateNarrativeFromPayload(event.narrative_payload);
      if (hydrated) {
        return {
          ...hydrated,
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
  accent: "ok" | "fail" | "qa";
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
): AdjudicatedParts | null {
  if (turn.responseKind === null) return null;
  if (turn.responseKind !== "build") {
    return {
      headline:
        turn.responseKind === "refuse"
          ? "Declined"
          : turn.responseKind === "diagnose"
            ? "Answered"
            : "Question",
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

export function computeTurnSummary(turn: TurnNarrativeState): TurnSummary {
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
      : adjudicatedSummaryParts(turn, {
          needsUntestedProposalReview,
          needsTestedProposalReview,
          hasEdited,
          hasDrafts,
          hasCleanCompletedBuild,
        });

  const headline = adjudicated
    ? adjudicated.headline
    : isStoppedWithDraft
      ? "Stopped with a draft"
      : isFail
        ? "Run halted"
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
