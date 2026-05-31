// Pure reducer + types for the workflow copilot turn-narrative bubble. Kept
// separate from NarrativeView.tsx so Vite Fast Refresh can hot-reload the
// component without re-evaluating reducer state, and so the reducer can be
// exercised under vitest without a JSX runtime.

import {
  CopilotResponseType,
  WorkflowCopilotBlockProgressUpdate,
  WorkflowCopilotDesignEndUpdate,
  WorkflowCopilotDesignStartUpdate,
  WorkflowCopilotNarrationUpdate,
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

export interface BlockState {
  workflowRunBlockId: string;
  label: string;
  blockType: string;
  state: BlockUIState;
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
  // Result success when kind is tool_result.
  success?: boolean;
  // Stable per-event id used as React key.
  id: string;
}

export interface TurnNarrativeState {
  turnId: string | null;
  turnIndex: number | null;
  mode: string;
  responseType: CopilotResponseType | null;
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
  designStarted: false,
  designEnded: false,
  draft: null,
  blocks: [],
  terminal: null,
  terminalMessage: null,
  narrativeSummary: null,
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

// Tool names we never surface in the user-facing activity log. Internal
// observation/maintenance tools are not interesting context for the user.
const ACTIVITY_TOOL_DENYLIST = new Set([
  "list_credentials",
  "get_run_results",
  "get_browser_screenshot",
]);

function buildActivityFromToolCall(
  event: WorkflowCopilotToolCallUpdate,
): ActivityEntry | null {
  if (ACTIVITY_TOOL_DENYLIST.has(event.tool_name)) {
    return null;
  }
  return {
    kind: "tool_call",
    text: `Calling ${event.tool_name}…`,
    iteration: event.iteration,
    toolName: event.tool_name,
    id: `tc-${event.tool_call_id}`,
  };
}

function buildActivityFromToolResult(
  event: WorkflowCopilotToolResultUpdate,
): ActivityEntry | null {
  if (ACTIVITY_TOOL_DENYLIST.has(event.tool_name)) {
    return null;
  }
  return {
    kind: "tool_result",
    text: event.summary || event.tool_name,
    iteration: event.iteration,
    toolName: event.tool_name,
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
    return {
      workflowRunBlockId:
        typeof obj.workflowRunBlockId === "string"
          ? obj.workflowRunBlockId
          : "",
      label: typeof obj.label === "string" ? obj.label : "",
      blockType: typeof obj.blockType === "string" ? obj.blockType : "task",
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
