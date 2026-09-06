import { Button } from "@/components/ui/button";
import {
  useState,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useCallback,
  memo,
} from "react";
import { getClient } from "@/api/AxiosClient";
import { ActionsApiResponse, getReadableActionType } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { CredentialsModal } from "@/routes/credentials/CredentialsModal";
import { CredentialModalTypes } from "@/routes/credentials/useCredentialModalState";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";
import {
  ReloadIcon,
  Cross2Icon,
  ChevronDownIcon,
  CheckIcon,
  ArrowUpIcon,
  Pencil1Icon,
} from "@radix-ui/react-icons";
import { createPortal } from "react-dom";
import { stringify as convertToYAML } from "yaml";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import { useCopilotHeaderStore } from "@/store/useCopilotHeaderStore";
import { WorkflowCreateYAMLRequest } from "@/routes/workflows/types/workflowYamlTypes";
import { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";
import { describeRecordedAction } from "@/routes/workflows/workflowBlockUtils";
import {
  isBlockItem,
  WorkflowRunTimelineItem,
} from "@/routes/workflows/types/workflowRunTypes";
import { toast } from "@/components/ui/use-toast";
import { getSseClient } from "@/api/sse";
import {
  WorkflowCopilotCancelRequest,
  WorkflowCopilotCancelSource,
  WorkflowCopilotChatHistoryMessage,
  WorkflowCopilotChatHistoryResponse,
  WorkflowCopilotDesignEndUpdate,
  WorkflowCopilotDesignStartUpdate,
  WorkflowCopilotProcessingUpdate,
  WorkflowCopilotStreamErrorUpdate,
  WorkflowCopilotStreamResponseUpdate,
  WorkflowCopilotToolCallUpdate,
  WorkflowCopilotToolResultUpdate,
  WorkflowCopilotCondensingUpdate,
  WorkflowCopilotNarrationUpdate,
  WorkflowCopilotBlockProgressUpdate,
  WorkflowCopilotRunStartedUpdate,
  WorkflowCopilotRunOutcomeUpdate,
  WorkflowCopilotTurnStartUpdate,
  WorkflowCopilotWorkflowDraftUpdate,
  WorkflowCopilotCredentialRequiredUpdate,
  WorkflowCopilotTitleUpdate,
  WorkflowCopilotChatSender,
  WorkflowCopilotChatRequest,
  WorkflowCopilotChatSummary,
  WorkflowCopilotClearProposedWorkflowRequest,
  WorkflowCopilotApplyProposedWorkflowRequest,
  WorkflowCopilotAudioUploadResponse,
  QuestionInteraction,
  QuestionResponse,
  WorkflowCopilotQuestionRequired,
  WorkflowCopilotQuestionResolved,
  CopilotProductAction,
} from "./workflowCopilotTypes";
import { WorkflowCopilotHistory } from "./WorkflowCopilotHistory";
import { SelectedBlockChip } from "./SelectedBlockChip";
import { readSelectedBlockLabel } from "./selectedBlockLabel";
import { selectAutoBoundReceiptIndexes } from "./autoBoundReceiptIndexes";
import { shouldWaitForLiveBrowser } from "./browserReadiness";
import {
  QueuedPromptReason,
  resolveDrainAction,
  resolveSendAction,
} from "./sendQueue";
import { shouldAutoApplyWorkflowResponse } from "./proposalDisposition";
import { shouldArmDraftingGapTimer } from "./copilotPhases";
import { InstantAckPlaceholder, NarrativeView } from "./NarrativeView";
import { CopilotMarkdown } from "./CopilotMarkdown";
import { CopilotWorkingStatus } from "./CopilotWorkingStatus";
import { useRunLifecycleAnnouncements } from "./useRunLifecycleAnnouncements";
import { ConfirmCard, shouldShowConfirmCard } from "./cards/ConfirmCard";
import { ConnectedAccountChoiceCard } from "./cards/ConnectedAccountChoiceCard";
import { QuestionPartsCard } from "./cards/QuestionPartsCard";
import { nextAnsweringMessage, previousAskingMessage } from "./cardAdjacency";
import { composerPlaceholder } from "./composerPlaceholder";
import { connectedAccountChoiceLabel } from "./cards/connectedAccountChoiceLabel";
import { shouldShowDiffCard } from "./cards/DiffCard";
import { ReviewGateCard, getReviewGateVerdict } from "./cards/ReviewGateCard";
import { GoogleReconnectCard } from "./cards/GoogleReconnectCard";
import {
  CredentialCard,
  type CredentialRequiredFrame,
  type CredentialRequiredReason,
  type CredentialPauseHistorical,
} from "./cards/CredentialCard";
import {
  CopilotBlockActionsEvent,
  EMPTY_NARRATIVE,
  NarrativeEvent,
  RecordedActionSummary,
  TurnNarrativeState,
  applyNarrativeEvent,
  hydrateHistoryNarrative,
  notConfirmedOutcome,
  parseUtcIsoMs,
} from "./narrativeState";
import { computeFollowSignature, useStickToBottom } from "./useStickToBottom";
import { useTurnActivityChange } from "./useTurnActivityChange";
import { useSpeechToTextField } from "@/hooks/useSpeechToTextField";
import { SpeechInputButton } from "@/components/SpeechInputButton";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from "@/components/ui/dropdown-menu";
import { cn, formatElapsedSeconds } from "@/util/utils";
import { ControlTooltip } from "@/routes/workflows/studio/ControlTooltip";
import {
  useReleaseStudioRun,
  useSwitchStudioRun,
} from "@/routes/workflows/studio/runSwitchNavigation";
import { searchWithSystemBlockFocus } from "@/routes/workflows/editor/hooks/useSelectedBlockUrlSync";
import { studioPanelId } from "@/routes/workflows/studio/constants";
import {
  liveLocationState,
  liveSearch,
} from "@/routes/workflows/studio/liveSearch";
import { resolveOpenPanes } from "@/routes/workflows/studio/panes";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useWorkflowBlockSearchStore } from "@/store/WorkflowBlockSearchStore";
import { resolveTimelineBlockJumpNodeId } from "@/routes/workflows/studio/runview/timelineBlockJump";
import { TooltipProvider } from "@/components/ui/tooltip";

// Cap on retained per-turn snap-back snapshots. A typical session has a
// handful of turns; this ceiling guards a runaway long-running chat.
const MAX_TURN_SNAPSHOTS = 20;
// A stream that closes with no terminal frame is usually a lost client
// connection while the server handler runs on to persist the real reply, so the
// ladder is sized to the server's own turn budget rather than to a short wait.
const RECOVERY_POLL_DELAYS_MS = [2_000, 3_000, 5_000, 8_000, 12_000, 20_000];
const RECOVERY_POLL_STEADY_MS = 30_000;
// The server's RECONCILE_ABANDON_AFTER_SECONDS is 1_320_000ms. The margin holds
// several 30s reads past it, so a turn whose reply was never persisted reaches
// both the read that produces its interrupted row and the later one that picks
// up the real reply if the turn finishes after all.
const RECOVERY_POLL_BUDGET_MS = 1_500_000;
const INTERRUPTED_TERMINAL_REASON = "interrupted";
const SEND_FAILED_MESSAGE = "Sorry, I encountered an error. Please try again.";
// A severed stream is usually the client losing the network, so recovery reads
// fail too. Few enough that an offline user gets the plain failure back in
// seconds; more than one so a single blip does not end a live recovery.
const RECOVERY_POLL_FAILURE_CEILING = 3;
// Reads spent watching for the real reply to replace an interrupted row. A turn
// cancelled operationally never produces one, so this is bounded well short of
// the budget.
const RECOVERY_POLL_SUPERSEDE_READS = 4;
// Says only what is true: the composer stays enabled while this is showing, so
// it must not claim a retry is being held back.
const RECOVERY_IN_PROGRESS_MESSAGE =
  "The connection dropped, so Copilot is checking whether this turn finished.";
const TEST_END_TO_END_PROMPT = "Test this workflow end to end.";
const diagnoseRunReceipt = (runId: string) =>
  `Diagnose run ${runId} and repair the workflow.`;

type ArmedProductAction =
  | { action: "test_end_to_end"; workflowRunId?: undefined }
  | { action: "diagnose_run"; workflowRunId: string };

// Cadence for re-fetching a live test run's recorded actions. Mirrors the
// backend block-status poll (5s) closely enough to surface rows soon after
// they land without hammering the timeline endpoint.
const RECORDED_ACTIONS_POLL_INTERVAL_MS = 2500;

function recordedActionDurationMs(action: ActionsApiResponse): number | null {
  const output = action.output;
  if (!output || typeof output !== "object" || Array.isArray(output)) {
    return null;
  }
  const durationMs = (output as Record<string, unknown>).duration_ms;
  return typeof durationMs === "number" ? durationMs : null;
}

function toRecordedActionSummary(
  action: ActionsApiResponse,
): RecordedActionSummary {
  return {
    actionId: action.action_id,
    label: getReadableActionType(action.action_type),
    // The chat has no workflow definition in scope, so rows resolve from the action
    // itself; the run-view timeline additionally matches the definition's step text.
    summary: describeRecordedAction(action, null),
    durationMs: recordedActionDurationMs(action),
    failed: action.status === "failed",
  };
}

// Timeline items nest branch/loop children; walk the whole tree so a
// conditional or loop body's blocks are not missed.
function collectTimelineBlockActions(
  items: ReadonlyArray<WorkflowRunTimelineItem>,
): Array<{ workflowRunBlockId: string; actions: ActionsApiResponse[] }> {
  const out: Array<{
    workflowRunBlockId: string;
    actions: ActionsApiResponse[];
  }> = [];
  for (const item of items) {
    if (isBlockItem(item)) {
      out.push({
        workflowRunBlockId: item.block.workflow_run_block_id,
        actions: item.block.actions ?? [],
      });
    }
    if (item.children.length > 0) {
      out.push(...collectTimelineBlockActions(item.children));
    }
  }
  return out;
}

// Build's color emoji is flattened to a tone-adaptive monochrome silhouette so
// it reads cleanly on the dark UI.
const BUILD_GLYPH = "\uD83D\uDC09";

// What the arming gate is actually for: the second click of a double-tap on Send, which
// lands on the morphed control a moment later. That is a sub-second gesture, so the
// window is sized to it — long enough to swallow the stray click, short enough that the
// visible Stop is not dead while a turn hangs. The first frame arms sooner and clears it.
const STOP_ARM_DOUBLE_TAP_MS = 500;

const STOP_UNCONFIRMED_NOTICE =
  "I sent the stop, but couldn't confirm what this turn recorded. Reload to see the turn's own report.";

// The request itself failed, so the stop never reached the backend and the turn may
// still be running. Saying "I sent the stop" here would claim something that did not happen.
const STOP_NOT_SENT_NOTICE =
  "I couldn't send the stop. Reload to see what this turn recorded.";

const STOP_ORBIT_GRADIENT =
  "conic-gradient(from 0deg, rgba(120,170,255,.08) 0deg, rgba(120,170,255,.08) 120deg, rgba(150,195,255,.55) 250deg, #dbeaff 330deg, rgba(120,170,255,.08) 360deg)";

function parseServerStamp(createdAt: string): number {
  const stamp = /(Z|[+-]\d{2}:?\d{2})$/.test(createdAt)
    ? createdAt
    : `${createdAt}Z`;
  const parsed = Date.parse(stamp);
  return Number.isFinite(parsed) ? parsed : NaN;
}

function findRecoveredRow(
  history: WorkflowCopilotChatHistoryMessage[],
  turnId: string,
  untaggedBaseline: number | null,
): WorkflowCopilotChatHistoryMessage | null {
  const byTurnId = history.find(
    (message) =>
      message.sender === "ai" &&
      message.turn_outcome?.copilot_turn_id === turnId,
  );
  if (byTurnId) {
    return byTurnId;
  }
  // turn_outcome is optional on a persisted row, so a reply written without one
  // carries no id to match. The baseline is a server stamp so both sides of this
  // comparison come from the same clock; a client clock here let skew adopt
  // another turn's row or suppress our own.
  if (untaggedBaseline === null) {
    return null;
  }
  const last = history[history.length - 1];
  if (!last || last.sender !== "ai" || last.turn_outcome?.copilot_turn_id) {
    return null;
  }
  return parseServerStamp(last.created_at) > untaggedBaseline ? last : null;
}

function isPictographic(glyph: string): boolean {
  try {
    return /\p{Extended_Pictographic}/u.test(glyph);
  } catch {
    return false;
  }
}

function ModeGlyph({
  tone = "light",
  glow = false,
}: {
  tone?: "light" | "dark";
  glow?: boolean;
}) {
  const glyph = BUILD_GLYPH;
  const filter = isPictographic(glyph)
    ? tone === "dark"
      ? "grayscale(1) brightness(0)"
      : "grayscale(1) brightness(0) invert(1)"
    : undefined;
  return (
    <span className="relative inline-flex h-[18px] w-[18px] items-center justify-center leading-none">
      {glow ? (
        <span
          aria-hidden="true"
          className="pointer-events-none absolute inset-[-5px] rounded-full"
          style={{
            background:
              "radial-gradient(circle, rgba(96,165,250,0.55) 0%, rgba(59,130,246,0.18) 45%, rgba(59,130,246,0) 72%)",
          }}
        />
      ) : null}
      <span className="relative text-[16px]" style={{ lineHeight: 1, filter }}>
        {glyph}
      </span>
    </span>
  );
}

export function ConvoAggregatePill({
  messages,
  isInFlight,
  hasPendingQuestion = false,
}: {
  messages: ChatMessage[];
  isInFlight: boolean;
  hasPendingQuestion?: boolean;
}) {
  const turnsWithNarrative = messages.filter(
    (m) => m.sender === "ai" && m.narrative,
  );
  if (turnsWithNarrative.length < 2) return null;
  let earliestMs: number | null = null;
  let latestMs: number | null = null;
  for (const m of turnsWithNarrative) {
    const startMs = parseUtcIsoMs(m.narrative?.startedAt);
    if (startMs !== null) {
      earliestMs =
        earliestMs === null ? startMs : Math.min(earliestMs, startMs);
    }
    const endMs =
      parseUtcIsoMs(m.narrative?.endedAt) ?? parseUtcIsoMs(m.timestamp);
    if (endMs !== null) {
      latestMs = latestMs === null ? endMs : Math.max(latestMs, endMs);
    }
  }
  const elapsedLabel =
    earliestMs !== null && latestMs !== null && latestMs > earliestMs
      ? formatElapsedSeconds(latestMs - earliestMs)
      : null;
  // An interrupted or user-cancelled turn carries terminal "error" without having
  // failed, so the session pill applies the same guard as the per-turn chip.
  const anyError = turnsWithNarrative.some(
    (m) => m.narrative?.terminal === "error" && !m.narrative?.cancelled,
  );
  const awaitingUser = hasPendingQuestion;
  // awaitingUser outranks anyError, which is session-wide: a question asked
  // after a failed build test is what the user acts on next.
  const status = isInFlight
    ? "In flight"
    : awaitingUser
      ? "Waiting on you"
      : anyError
        ? "Halted"
        : "Done";
  const dotClass = isInFlight
    ? "bg-blue-400"
    : awaitingUser
      ? "bg-amber-400"
      : anyError
        ? "bg-rose-400"
        : "bg-emerald-400";
  return (
    <div className="flex justify-center pb-1">
      <span className="inline-flex items-center gap-2 rounded-full border border-border bg-slate-elevation1/60 px-3 py-0.5 text-[11px] text-tertiary-foreground">
        <span
          aria-hidden="true"
          className={`inline-block h-1.5 w-1.5 rounded-full ${dotClass}`}
        />
        {turnsWithNarrative.length} turns
        {elapsedLabel ? ` · ${elapsedLabel} elapsed` : ""}
        {" · "}
        {status}
      </span>
    </div>
  );
}

export interface ChatMessage {
  id: string;
  sender: WorkflowCopilotChatSender;
  content: string;
  timestamp?: string;
  // frozen narrative-bubble state captured at terminal RESPONSE
  // so the per-block cards persist as the user scrolls back through past
  // turns. Live in-flight narrative is rendered separately at the bottom.
  narrative?: TurnNarrativeState;
  // FE-synthetic rows (never persisted, never sent to the LLM).
  kind?: "run_lifecycle" | "status_notice";
}

function connectedAccountSelectionReceipt(
  messages: ChatMessage[],
  index: number,
): string | null {
  const message = messages[index];
  const prior = previousAskingMessage(messages, index);
  if (
    message?.sender !== "user" ||
    prior?.sender !== "ai" ||
    !prior.narrative
  ) {
    return null;
  }
  const choiceSets = [
    prior.narrative.connectedAccountChoices,
    ...prior.narrative.googleConnectionNotices.map(
      (notice) => notice.choices ?? [],
    ),
  ];
  for (const choices of choiceSets) {
    const selected = choices.find(
      (choice) => choice.connection_id === message.content,
    );
    if (selected) {
      return `Selected ${selected.name} — ${connectedAccountChoiceLabel(selected, choices)}`;
    }
  }
  return null;
}

const getLatestDiffCardTurnId = (messages: ChatMessage[]): string | null => {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const narrative = messages[index]?.narrative;
    if (narrative?.turnId && shouldShowDiffCard(narrative)) {
      return narrative.turnId;
    }
  }
  return null;
};

// messages.length - 1 with any trailing run_lifecycle lines skipped, so
// proposal actions keep attaching to the last real turn.
const findLastTurnIndex = (messages: ChatMessage[]): number => {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index]?.kind !== "run_lifecycle") {
      return index;
    }
  }
  return -1;
};

// Locates the message owning a bypassed pending proposal so its gate keeps
// rendering actionable controls even after later turns push it up the thread.
const findLastIndexOfTurn = (
  messages: ChatMessage[],
  turnId: string,
): number => {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index]?.narrative?.turnId === turnId) {
      return index;
    }
  }
  return -1;
};

type QueuedPrompt = {
  selectedConnectedAccountId?: string;
  id: string;
  content: string;
  reason: QueuedPromptReason;
  audioBlob?: Blob | null;
  idempotencyKey?: string;
};

type SendOptions = {
  selectedConnectedAccountId?: string;
  queuedMessageId?: string;
  skipQueue?: boolean;
  audioBlob?: Blob | null;
  idempotencyKey?: string;
};

type WorkflowCopilotSsePayload =
  | WorkflowCopilotProcessingUpdate
  | WorkflowCopilotStreamResponseUpdate
  | WorkflowCopilotStreamErrorUpdate
  | WorkflowCopilotToolCallUpdate
  | WorkflowCopilotToolResultUpdate
  | WorkflowCopilotCondensingUpdate
  | WorkflowCopilotNarrationUpdate
  | WorkflowCopilotBlockProgressUpdate
  | WorkflowCopilotRunStartedUpdate
  | WorkflowCopilotRunOutcomeUpdate
  | WorkflowCopilotTurnStartUpdate
  | WorkflowCopilotDesignStartUpdate
  | WorkflowCopilotDesignEndUpdate
  | WorkflowCopilotWorkflowDraftUpdate
  | WorkflowCopilotTitleUpdate
  | WorkflowCopilotCredentialRequiredUpdate
  | WorkflowCopilotQuestionRequired
  | WorkflowCopilotQuestionResolved;

// The live pause frame is a structural superset of the card's frame; only
// reason needs narrowing (the card tolerates unknown tokens either way).
function liveFrameToCardFrame(
  frame: WorkflowCopilotCredentialRequiredUpdate,
): CredentialRequiredFrame {
  return { ...frame, reason: frame.reason as CredentialRequiredReason };
}

// Terminal-mode synthetic frame from the sparse narrative_payload signals.
// credentialPause wins over credentialPrompt so a paused-then-resolved turn
// shows one resolved card, not a receipt stacked with a fresh actionable
// prompt (the timeout/dedup case). "declined" → no card was ever shown.
function credentialCardFrameFor(
  turn: TurnNarrativeState,
): CredentialRequiredFrame | null {
  if (turn.credentialPause) {
    if (turn.credentialPause.outcome === "declined") return null;
    return {
      type: "credential_required",
      reason: (turn.credentialPrompt?.reason ??
        "workflow_credential_inputs_unbound") as CredentialRequiredReason,
    };
  }
  if (turn.credentialPrompt) {
    return {
      type: "credential_required",
      reason: turn.credentialPrompt.reason as CredentialRequiredReason,
    };
  }
  return null;
}

// A co-occurring credential ask/pause owns this turn's credential UI and its
// credentialResolutions[turnId] entry; the auto-bind receipt would double up and
// mis-adopt that ask's resolution, so it defers whenever a card frame exists.
function autoBoundReceiptFor(
  message: ChatMessage,
): TurnNarrativeState["credentialAutoBound"] {
  const turn = message.narrative;
  if (message.sender !== "ai" || !turn || turn.turnId === null) return null;
  return credentialCardFrameFor(turn) ? null : turn.credentialAutoBound;
}

function historicalCredentialOutcome(
  turn: TurnNarrativeState,
): CredentialPauseHistorical | undefined {
  const pause = turn.credentialPause;
  if (!pause || pause.outcome === "declined") return undefined;
  return {
    outcome: pause.outcome,
    credentialId: pause.credentialId ?? undefined,
  };
}

type CredentialResolution = CredentialPauseHistorical & {
  name?: string;
  // Terminal connect auto-sent a "continue" turn — drives the receipt copy.
  continued?: boolean;
};

// Append a resolution keyed by turn, capping the map with oldest-eviction like
// the sibling per-turn maps (turnSnapshots/turnOwnedRunIds). delete-then-set
// re-inserts an existing turn as newest so an active turn isn't evicted.
function withCappedResolution(
  prev: Record<string, CredentialResolution>,
  turnId: string,
  value: CredentialResolution,
): Record<string, CredentialResolution> {
  const next = { ...prev };
  delete next[turnId];
  next[turnId] = value;
  const keys = Object.keys(next);
  for (const key of keys.slice(
    0,
    Math.max(0, keys.length - MAX_TURN_SNAPSHOTS),
  )) {
    delete next[key];
  }
  return next;
}

const formatChatTimestamp = (value: string) => {
  let normalizedValue = value.replace(/\.(\d{3})\d*/, ".$1");
  if (!normalizedValue.endsWith("Z")) {
    normalizedValue += "Z";
  }
  return new Date(normalizedValue).toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
  });
};

interface MessageItemProps {
  message: ChatMessage;
  footer?: React.ReactNode;
  // Replaces the timestamp with a spinner + cancel affordance while this
  // message is the currently-queued (not yet sent) prompt.
  queuedStatus?: { text: string; onCancel: () => void } | null;
}

const MessageItem = memo(
  ({ message, footer, queuedStatus }: MessageItemProps) => {
    const queuedFooter = queuedStatus ? (
      <div className="mt-2 flex items-center gap-1.5 border-t border-white/10 pt-2 text-[11.5px] text-muted-foreground">
        <ReloadIcon className="h-3 w-3 shrink-0 animate-spin" />
        <span className="min-w-0 flex-1 truncate">{queuedStatus.text}</span>
        <button
          type="button"
          onClick={queuedStatus.onCancel}
          title="Edit queued message"
          aria-label="Edit queued message"
          className="shrink-0 rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
        >
          <Cross2Icon className="h-3 w-3" />
        </button>
      </div>
    ) : null;
    if (message.sender === "user") {
      return (
        <div className="flex justify-end">
          <div className="max-w-[85%] rounded-xl border border-white/5 bg-slate-elevation4 px-3.5 py-2.5 text-[13.5px] leading-[1.5] text-foreground">
            <div className="flex items-end gap-2">
              <p className="min-w-0 flex-1 whitespace-pre-wrap [overflow-wrap:anywhere]">
                {message.content}
              </p>
              {!queuedStatus && message.timestamp ? (
                <span className="pointer-events-none shrink-0 rounded bg-slate-elevation1/70 px-1.5 py-0.5 text-[10px] text-muted-foreground">
                  {formatChatTimestamp(message.timestamp)}
                </span>
              ) : null}
            </div>
            {queuedFooter}
          </div>
        </div>
      );
    }
    if (message.sender === "product") {
      return (
        <div className="flex flex-col">
          <RunLifecycleLine content={message.content} />
          {queuedFooter}
        </div>
      );
    }
    return (
      <div className="flex flex-col gap-2">
        <div className="pl-1 text-[13px] leading-[1.55] text-foreground dark:text-slate-200">
          <CopilotMarkdown text={message.content} />
        </div>
        {footer ? (
          <div className="flex flex-wrap gap-2 pl-1">{footer}</div>
        ) : null}
      </div>
    );
  },
);

// Studio-only run status line, distinct from ai prose: no bubble, no footer.
function RunLifecycleLine({ content }: { content: string }) {
  return (
    <div
      className="flex items-center gap-2 pl-1 text-xs text-muted-foreground"
      role="status"
      aria-live="polite"
    >
      <span
        aria-hidden="true"
        className="inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-slate-500"
      />
      <span>{content}</span>
    </div>
  );
}

// `persisted` true = atomic accept (server already wrote new version); false/undefined = local edit.
// `applied` marks a turn's accepted terminal apply; drafts and snap-backs omit it.
export type WorkflowUpdateOptions = {
  persisted?: boolean;
  applied?: boolean;
  // A mid-turn draft lands while the user may be renaming the agent, so its title is
  // applied only if nothing has named it yet. Discrete applies (accept, snap-back)
  // are authoritative and keep the force path.
  midTurnDraft?: boolean;
};

interface WorkflowCopilotChatProps {
  onWorkflowUpdate?: (
    workflow: WorkflowApiResponse,
    options?: WorkflowUpdateOptions,
  ) => void;
  onReviewWorkflow?: (
    workflow: WorkflowApiResponse,
    clearPending: () => void,
  ) => void;
  // parent receives the block label when the user clicks a block
  // card in the narrative bubble. The editor uses this to flash-highlight
  // the matching canvas node.
  onBlockSelect?: (blockLabel: string) => void;
  isOpen?: boolean;
  onClose?: () => void;
  onMessageCountChange?: (count: number) => void;
  onTurnActivityChange?: (active: boolean) => void;
  buttonRef?: React.RefObject<HTMLButtonElement>;
  liveBrowserSessionId?: string | null;
  workflowRunId?: string | null;
  requiresLiveBrowser?: boolean;
  isLiveBrowserReady?: boolean;
  initialMessage?: string;
  initialAction?: CopilotProductAction;
  onInitialMessageConsumed?: () => void;
  // Render as a docked panel (no float/drag/resize) instead of a floating window.
  docked?: boolean;
  // Render frameless — no border, background, or title; the header keeps only
  // the controls row. Only used when `docked`.
  chromeless?: boolean;
  // When docked, render into this element via a portal (keeps the component in
  // its parent's React tree so canvas callbacks stay wired) instead of inline.
  portalTarget?: HTMLElement | null;
}

// Snap-back state keyed by turn_id so rapid resubmits don't clobber a prior
// turn's snapshot before its terminal frame lands. The snapshot captures
// pre-submit canvas state (including unsaved local edits) so Reject / Cancel /
// ERROR can revert exactly what the user submitted.
interface TurnSnapshot {
  snapshot: WorkflowApiResponse | null;
  hadStagedDraft: boolean;
}

const AUTO_SEND_TIMEOUT_MS = 5000;

const DEFAULT_WINDOW_WIDTH = 600;
const DEFAULT_WINDOW_HEIGHT = 400;
const MIN_WINDOW_WIDTH = 300;
const MIN_WINDOW_HEIGHT = 300;
const OFFSET = 24;

const calculateDefaultPosition = (
  width: number,
  height: number,
  buttonRef?: React.RefObject<HTMLButtonElement>,
) => {
  // If button ref is available, align left edge of window with left edge of button
  if (buttonRef?.current) {
    const buttonRect = buttonRef.current.getBoundingClientRect();
    return {
      x: buttonRect.left - OFFSET,
      y: window.innerHeight - height - 2 * OFFSET,
    };
  }

  // Fallback to centered position
  return {
    x: window.innerWidth / 2 - width / 2,
    y: window.innerHeight - height - 2 * OFFSET,
  };
};

const constrainPosition = (
  x: number,
  y: number,
  width: number,
  height: number,
) => {
  const maxX = window.innerWidth - width - OFFSET;
  const maxY = window.innerHeight - height - OFFSET;

  return {
    x: Math.min(Math.max(0, x), maxX),
    y: Math.min(Math.max(0, y), maxY),
  };
};

export function WorkflowCopilotChat({
  onWorkflowUpdate,
  onReviewWorkflow,
  onBlockSelect,
  isOpen = true,
  onClose,
  onMessageCountChange,
  onTurnActivityChange,
  buttonRef,
  liveBrowserSessionId,
  workflowRunId: workflowRunIdProp,
  requiresLiveBrowser = false,
  isLiveBrowserReady = false,
  initialMessage,
  initialAction,
  onInitialMessageConsumed,
  docked = false,
  chromeless = false,
  portalTarget,
}: WorkflowCopilotChatProps = {}) {
  const codeBlockModeFlag = useFeatureFlag("WORKFLOW_COPILOT_CODE_BLOCK_MODE");
  const codeBlockAccessFlag = useFeatureFlag("CODE_BLOCK_ACCESS");
  const codeBlockModeEnabled =
    codeBlockModeFlag === true && codeBlockAccessFlag === true;
  const codeFirstAccessible = codeBlockModeEnabled;
  const [codeWorkflow, setCodeWorkflow] = useState(codeFirstAccessible);
  const [codeBlockRequestOverride, setCodeBlockRequestOverride] = useState<
    boolean | null
  >(codeFirstAccessible);
  // Flags arrive asynchronously from /customer; seed the default once they resolve, never again.
  const composerSeededRef = useRef(false);
  const flagsResolved =
    codeBlockModeFlag !== undefined && codeBlockAccessFlag !== undefined;
  useEffect(() => {
    if (composerSeededRef.current || !flagsResolved) {
      return;
    }
    composerSeededRef.current = true;
    setCodeWorkflow(codeFirstAccessible);
    setCodeBlockRequestOverride(codeFirstAccessible);
  }, [codeFirstAccessible, flagsResolved]);
  // "Build with code" is offered as a second Build implementation in the
  // dropdown rather than a separate toggle.
  const codeOptionAvailable = codeBlockModeEnabled;
  const codeStateActive = codeWorkflow && codeOptionAvailable;
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [proposedWorkflow, setProposedWorkflow] =
    useState<WorkflowApiResponse | null>(null);
  // Owning turn of the current proposedWorkflow. Kept alongside it (never
  // merged into one object) so the gate can re-attach to its owning message.
  const [pendingProposalTurnId, setPendingProposalTurnId] = useState<
    string | null
  >(null);
  // Transient ring highlight on the gate the pending-proposal chip just
  // scrolled to; cleared after the flash window.
  const [gateFlashTurnId, setGateFlashTurnId] = useState<string | null>(null);
  // Turn IDs the user explicitly rejected. This is client-local because reject
  // only reverts the local canvas; the backend proposalDisposition stays fixed.
  const [rejectedTurnIds, setRejectedTurnIds] = useState<Set<string>>(
    new Set(),
  );
  // Mirror of rejectedTurnIds for manual Accept, session-local (no server
  // record of a non-auto-applied accept).
  const [acceptedTurnIds, setAcceptedTurnIds] = useState<Set<string>>(
    new Set(),
  );
  const [autoAccept, setAutoAccept] = useState<boolean>(false);
  const [inputValue, setInputValue] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  // A stop is a round trip to the backend; without this the control stays
  // live-looking and users press it repeatedly.
  const [isStopping, setIsStopping] = useState(false);
  const [stopArmed, setStopArmed] = useState(false);
  useTurnActivityChange(isLoading, onTurnActivityChange);
  const [queuedPrompt, setQueuedPrompt] = useState<QueuedPrompt | null>(null);
  const [narrative, setNarrative] =
    useState<TurnNarrativeState>(EMPTY_NARRATIVE);
  // mirror of the latest narrative state so async SSE handlers
  // closed over `handleSend`'s scope can read the live value instead of the
  // stale closure capture from submit time.
  const narrativeRef = useRef<TurnNarrativeState>(EMPTY_NARRATIVE);
  useEffect(() => {
    narrativeRef.current = narrative;
  }, [narrative]);
  const applyStoredNarrativeEvent = useCallback(
    (event: NarrativeEvent, base?: TurnNarrativeState) => {
      const next = applyNarrativeEvent(base ?? narrativeRef.current, event);
      narrativeRef.current = next;
      setNarrative(next);
      return next;
    },
    [],
  );
  const [isLoadingHistory, setIsLoadingHistory] = useState(false);
  // Active mid-build credential pause frame for the in-flight turn. Cleared at
  // turn_start and at every terminal so a dead resume_token can never render.
  const [livePauseFrame, setLivePauseFrame] =
    useState<WorkflowCopilotCredentialRequiredUpdate | null>(null);
  // Local credential-card resolutions keyed by turn_id: live pauses after a
  // successful resume POST, and terminal-mode connect/skip (which never POST).
  // name is captured at connect so the receipt keeps showing it after the turn
  // goes terminal and the live frame (and its matching list) is gone.
  const [credentialResolutions, setCredentialResolutions] = useState<
    Record<string, CredentialResolution>
  >({});
  // Terminal asks whose auto-continue send failed: the optimistic "connected"
  // receipt is rolled back and the ask is forced actionable again (it is no
  // longer the tail) so the user can re-pick instead of hitting a dead end.
  const [strandedTerminalContinuations, setStrandedTerminalContinuations] =
    useState<ReadonlySet<string>>(() => new Set());
  const [credentialModalOpen, setCredentialModalOpen] = useState(false);
  // Bumped when a credential is created so the card's picker re-fetches and includes the new one.
  const [credentialsReloadKey, setCredentialsReloadKey] = useState(0);
  // Routes the add-credential modal's onCredentialCreated back to the card that
  // opened it: a live frame (POST resume) or a terminal turn (local resolve).
  // isLastMessage is captured at open time so a terminal connect only
  // auto-continues from the conversation's tail, not a scrolled-back card.
  const pendingCredentialConnect = useRef<{
    frame: WorkflowCopilotCredentialRequiredUpdate | null;
    turnId: string;
    isLastMessage: boolean;
  } | null>(null);
  // Original ask turnId whose auto-continue send is in flight, so a stream
  // failure can roll its optimistic resolution back. Cleared in handleSend's
  // finally; only one continuation is ever pending (sends are serialized).
  const pendingTerminalContinuation = useRef<string | null>(null);
  // Latest handleSend for callbacks defined before it (the terminal
  // auto-continue fires from the modal's onCredentialCreated).
  const handleSendRef = useRef<((messageOverride?: string) => void) | null>(
    null,
  );
  // Single-flight guard for the resume POST against a double-click.
  const credentialResponseInFlight = useRef(false);
  const streamingAbortController = useRef<AbortController | null>(null);
  // Synchronous in-flight gate. State (isLoading) lags a render behind, so a
  // rapid double-submit would run a stale closure and start a second stream;
  // this ref is set before the first await and read at the top of handleSend.
  const inFlightRef = useRef(false);
  // Counts sends rather than tracking one in progress: a send that both starts
  // and finishes inside a recovery read leaves inFlightRef false at either end
  // of it, and applying history fetched before that send would drop its turn.
  const sendEpoch = useRef(0);
  // This latch closes the same-render double-click window before React can
  // publish isLoading. Once a selection queues behind another turn, isLoading
  // keeps every account row disabled until that queue drains.
  const connectedAccountChoiceLatch = useRef<string | null>(null);
  const [
    connectedAccountChoicePendingTurnId,
    setConnectedAccountChoicePendingTurnId,
  ] = useState<string | null>(null);
  // Synchronous mirror of queuedPrompt (like inFlightRef) so a same-tick double
  // submit can't queue twice and orphan the first message. Set via updateQueuedPrompt.
  const queuedPromptRef = useRef<QueuedPrompt | null>(null);
  // What the turn was asked to do. completedNormally starts false and is set
  // only on a clean terminal, so every other exit drains the queue as before.
  const lastTurnRef = useRef<{
    content: string;
    workflowPermanentId: string | undefined;
    hadAudio: boolean;
    hadBlockTarget: boolean;
    browserSessionId: string | null;
    codeBlock: boolean | null;
    completedNormally: boolean;
  } | null>(null);
  const pendingMessageId = useRef<string | null>(null);
  const pendingCancelToken = useRef<string | null>(null);
  // Read by cancelSend, which must not re-create on every render of the arming signal.
  const turnObservablyRunningRef = useRef(false);
  const stopArmTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const armStop = useCallback(() => {
    if (stopArmTimer.current !== null) {
      clearTimeout(stopArmTimer.current);
      stopArmTimer.current = null;
    }
    setStopArmed(true);
  }, []);
  const cancelSafetyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const gateFlashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Backend cancel watcher polls Redis: a turn can complete normally between
  // the cancel POST and the watcher firing, so the frontend must remember it.
  const cancelInFlightController = useRef<AbortController | null>(null);
  const recoveryPolls = useRef(new Map<string, () => void>());
  const recoveryGeneration = useRef(0);
  // The poll is declared before the reconcile it calls on a recovered row.
  const reconcileCanonicalWorkflowRef = useRef<(() => Promise<void>) | null>(
    null,
  );
  const [workflowCopilotChatId, setWorkflowCopilotChatId] = useState<
    string | null
  >(null);
  const [questionInteractions, setQuestionInteractions] = useState<
    QuestionInteraction[]
  >([]);
  const [questionCancelToken, setQuestionCancelToken] = useState<string | null>(
    null,
  );
  // Mirrors workflowCopilotChatId for async handlers that would otherwise
  // close over a stale value across renders (e.g. clearProposedWorkflow).
  const workflowCopilotChatIdRef = useRef<string | null>(null);
  const turnSnapshots = useRef<Map<string, TurnSnapshot>>(new Map());
  // Snapshot captured at submit time. Moved into turnSnapshots once
  // turn_start lands and we know the BE-assigned turn_id.
  const pendingSubmitSnapshot = useRef<WorkflowApiResponse | null>(null);
  // Most recent turn_id observed via turn_start; used by Reject and by
  // legacy error frames that don't carry a turn_id.
  const latestTurnId = useRef<string | null>(null);
  // Active recorded-action timeline polls keyed by workflow_run_id, and the
  // ids whose poll already converged on a terminal verdict. Together they stop
  // a run from being polled twice and from restarting after it finalized.
  const actionPollRef = useRef<Map<string, number>>(new Map());
  const finalizedActionRunIds = useRef<Set<string>>(new Set());
  // Run ids the copilot claimed via run_outcome — the turn narrates these
  // itself, so useRunLifecycleAnnouncements suppresses their lifecycle lines by
  // identity (an unrelated run seen in the same window must still be narrated).
  const turnOwnedRunIds = useRef<Set<string>>(new Set());
  const rememberTurnOwnedRun = useCallback((runId: string) => {
    const owned = turnOwnedRunIds.current;
    owned.add(runId);
    while (owned.size > MAX_TURN_SNAPSHOTS) {
      const oldest = owned.values().next().value;
      if (oldest === undefined) break;
      owned.delete(oldest);
    }
  }, []);
  // The run this turn pointed the studio's Browser pane at, so the focus is
  // written once per run and released only if we still own it.
  const focusedTurnRunId = useRef<string | null>(null);
  // Build-follow: while a docked turn streams, the canvas follows the block the
  // copilot is working on. Any pointer press outside the copilot pane hands
  // control back to the user for the rest of the turn (log-tail semantics).
  const buildFollowEngaged = useRef(false);
  const lastFollowedLabelRef = useRef<string | null>(null);
  // Focusing the turn's run is the copilot acting for the user, not a
  // navigation they asked for, so it must not add a Back step.
  const switchStudioRun = useSwitchStudioRun({
    replace: true,
    systemFocus: true,
  });
  const releaseStudioRun = useReleaseStudioRun();
  useEffect(() => {
    workflowCopilotChatIdRef.current = workflowCopilotChatId;
  }, [workflowCopilotChatId]);
  useEffect(() => {
    const activePolls = actionPollRef.current;
    const activeRecoveryPolls = recoveryPolls.current;
    return () => {
      streamingAbortController.current?.abort();
      activePolls.forEach((timer) => clearInterval(timer));
      activePolls.clear();
      if (cancelSafetyTimer.current !== null) {
        clearTimeout(cancelSafetyTimer.current);
        cancelSafetyTimer.current = null;
      }
      if (stopArmTimer.current !== null) {
        clearTimeout(stopArmTimer.current);
        stopArmTimer.current = null;
      }
      if (gateFlashTimer.current !== null) {
        clearTimeout(gateFlashTimer.current);
        gateFlashTimer.current = null;
      }
      recoveryGeneration.current += 1;
      activeRecoveryPolls.forEach((stop) => stop());
      activeRecoveryPolls.clear();
    };
  }, []);
  const [size, setSize] = useState({
    width: DEFAULT_WINDOW_WIDTH,
    height: DEFAULT_WINDOW_HEIGHT,
  });
  const [position, setPosition] = useState(
    calculateDefaultPosition(
      DEFAULT_WINDOW_WIDTH,
      DEFAULT_WINDOW_HEIGHT,
      buttonRef,
    ),
  );
  const [isDragging, setIsDragging] = useState(false);
  const [dragStart, setDragStart] = useState({ x: 0, y: 0 });
  const [isResizing, setIsResizing] = useState(false);
  const [resizeDirection, setResizeDirection] = useState<
    "n" | "s" | "e" | "w" | "se" | "sw" | "ne" | "nw"
  >("se");
  const [resizeStart, setResizeStart] = useState({
    x: 0,
    y: 0,
    width: 0,
    height: 0,
    posX: 0,
    posY: 0,
  });
  const credentialGetter = useCredentialGetter();
  const { workflowRunId: routeWorkflowRunId } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const workflowPermanentId = useWorkflowPermanentId();
  // The studio focuses a run via ?wr= (not a path param), so the route param is
  // empty there; an explicit prop grounds the chat in that run and wins.
  const workflowRunId = workflowRunIdProp ?? routeWorkflowRunId;
  const announceRunLifecycle = useCallback((message: ChatMessage) => {
    setMessages((prev) =>
      prev.some((existing) => existing.id === message.id)
        ? prev
        : [...prev, message],
    );
  }, []);
  useRunLifecycleAnnouncements({
    workflowRunId: docked ? workflowRunId : undefined,
    // isLoading here, not inFlightRef: this hook needs a value React re-runs
    // its effect on when the turn ends, which a ref can't do. The one-render
    // lag that matters for the double-submit guard above doesn't matter here.
    turnInFlight: isLoading,
    turnOwnedRunIds,
    announce: announceRunLifecycle,
  });
  // Recorded actions stream into the run timeline as the test run executes.
  // Fetch them repeatedly while a run is live (started from the earliest frame
  // that carries the run id: block_progress on a new backend, run_outcome on an
  // old one), so rows resolve during execution instead of only at adjudication.
  // The reducer merges by actionId (idempotent + grow), so re-fetching the same
  // growing set never duplicates rows and the terminal fetch just converges.
  const fetchRecordedActions = useCallback(
    async (runId: string) => {
      if (!workflowPermanentId) return;
      try {
        const client = await getClient(credentialGetter);
        const response = await client.get<WorkflowRunTimelineItem[]>(
          `/workflows/${workflowPermanentId}/runs/${runId}/timeline`,
        );
        const blocks = collectTimelineBlockActions(response.data ?? [])
          .filter((entry) => entry.actions.length > 0)
          .map((entry) => ({
            workflowRunBlockId: entry.workflowRunBlockId,
            // The API returns actions newest-first; replay must run oldest-first.
            actions: [...entry.actions].reverse().map(toRecordedActionSummary),
          }));
        if (blocks.length === 0) return;
        const event: CopilotBlockActionsEvent = {
          type: "client_block_actions",
          blocks,
          receivedAtMs: Date.now(),
        };
        applyStoredNarrativeEvent(event);
        // The fetch can resolve after the terminal response already froze a
        // snapshot into an AI message; patch it in place instead of
        // delaying the terminal render on this network call.
        setMessages((prev) =>
          prev.map((message) => {
            if (!message.narrative) return message;
            const next = applyNarrativeEvent(message.narrative, event);
            return next === message.narrative
              ? message
              : { ...message, narrative: next };
          }),
        );
      } catch (error) {
        // Best-effort enrichment — the card already shows the real run
        // outcome without a recorded-action replay if this fails.
        console.error("Failed to fetch recorded actions:", error);
      }
    },
    [applyStoredNarrativeEvent, credentialGetter, workflowPermanentId],
  );
  const finalizeRecordedActionsPoll = useCallback((runId: string) => {
    const timer = actionPollRef.current.get(runId);
    if (timer !== undefined) {
      clearInterval(timer);
      actionPollRef.current.delete(runId);
    }
    const done = finalizedActionRunIds.current;
    done.add(runId);
    while (done.size > MAX_TURN_SNAPSHOTS) {
      const oldest = done.values().next().value;
      if (oldest === undefined) break;
      done.delete(oldest);
    }
  }, []);
  const startRecordedActionsPoll = useCallback(
    (runId: string | null | undefined) => {
      if (!runId || !workflowPermanentId) return;
      if (
        actionPollRef.current.has(runId) ||
        finalizedActionRunIds.current.has(runId)
      ) {
        return;
      }
      const timers = actionPollRef.current;
      while (timers.size >= MAX_TURN_SNAPSHOTS) {
        const oldest = timers.entries().next().value;
        if (oldest === undefined) break;
        clearInterval(oldest[1]);
        timers.delete(oldest[0]);
      }
      void fetchRecordedActions(runId);
      timers.set(
        runId,
        window.setInterval(
          () => void fetchRecordedActions(runId),
          RECORDED_ACTIONS_POLL_INTERVAL_MS,
        ),
      );
    },
    [fetchRecordedActions, workflowPermanentId],
  );
  const stopAllRecordedActionsPolls = useCallback(() => {
    actionPollRef.current.forEach((timer) => clearInterval(timer));
    actionPollRef.current.clear();
  }, []);
  const focusTurnRun = useCallback(
    (runId: string | null | undefined) => {
      if (!docked || !runId) return;
      if (focusedTurnRunId.current === runId) return;
      focusedTurnRunId.current = runId;
      switchStudioRun(runId);
    },
    [docked, switchStudioRun],
  );
  const releaseTurnRun = useCallback(
    (runId?: string) => {
      const focused = focusedTurnRunId.current;
      if (focused === null || (runId !== undefined && runId !== focused)) {
        return;
      }
      focusedTurnRunId.current = null;
      releaseStudioRun(focused);
    },
    [releaseStudioRun],
  );
  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      if (!buildFollowEngaged.current) return;
      const target = event.target;
      if (
        !(target instanceof Element) ||
        !target.closest(`#${studioPanelId("copilot")}`)
      ) {
        buildFollowEngaged.current = false;
      }
    };
    window.addEventListener("pointerdown", onPointerDown, true);
    return () => window.removeEventListener("pointerdown", onPointerDown, true);
  }, []);
  const followBuildLabel = useCallback(
    (label: string | null) => {
      if (!docked || !label) return;
      const handle = useWorkflowBlockSearchStore.getState().handle;
      if (!handle) return;
      // Follow is a courtesy, so every guard errs toward not moving: the user
      // disengaged (touched the studio), recording mode owns the panes, or the
      // frame repeats the block already followed. resolveTimelineBlockJumpNodeId
      // adds the editor-open and unique-canvas-match rules.
      if (
        !buildFollowEngaged.current ||
        useRecordingStore.getState().isRecording ||
        label === lastFollowedLabelRef.current
      ) {
        return;
      }
      const nodeId = resolveTimelineBlockJumpNodeId({
        editorOpen: resolveOpenPanes(window.location.search).includes("editor"),
        targets: handle.getTargets(),
        label,
      });
      if (nodeId === null) return;
      lastFollowedLabelRef.current = label;
      // Mark the selection before it exists: focusBlock selects on the canvas
      // and useSelectedBlockUrlSync mirrors that into ?selected-block=, merging
      // against the live URL — the marker has to already be there, or the run
      // pin reads the follow as a block the user picked.
      navigate(
        {
          pathname: location.pathname,
          search: searchWithSystemBlockFocus(
            liveSearch(location.search),
            label,
          ),
          hash: location.hash,
        },
        // Same preservation useStudioPanes applies: a follow must not drop the
        // studio's location state (the copilot seed message rides in it).
        {
          replace: true,
          state: liveLocationState(location.search, location.state),
        },
      );
      handle.focusBlock(nodeId);
    },
    [
      docked,
      navigate,
      location.pathname,
      location.search,
      location.hash,
      location.state,
    ],
  );
  const respondToCredentialPause = useCallback(
    async (
      frame: WorkflowCopilotCredentialRequiredUpdate,
      action: "connected" | "skip",
      credentialId?: string,
      name?: string,
    ) => {
      if (credentialResponseInFlight.current) return;
      credentialResponseInFlight.current = true;
      try {
        // Copilot routes live on base_router (no /api/v1 prefix), like cancel.
        const client = await getClient(credentialGetter, "sans-api-v1");
        await client.post("/workflow/copilot/credential-response", {
          turn_id: frame.turn_id,
          workflow_copilot_chat_id: frame.workflow_copilot_chat_id,
          resume_token: frame.resume_token,
          action,
          credential_id: action === "connected" ? credentialId : undefined,
        });
        setCredentialResolutions((prev) =>
          withCappedResolution(
            prev,
            frame.turn_id,
            action === "connected"
              ? { outcome: "connected", credentialId, name }
              : { outcome: "skipped" },
          ),
        );
      } catch (error) {
        // Log only the message: the AxiosError serializes config.data, which
        // carries the one-time resume_token, into the console otherwise.
        console.error(
          "Failed to send credential response:",
          error instanceof Error ? error.message : String(error),
        );
        toast({
          title: "Couldn't send your credential response",
          description: "Please try again.",
          variant: "destructive",
        });
      } finally {
        credentialResponseInFlight.current = false;
      }
    },
    [credentialGetter],
  );
  // Terminal-mode cards have no resume_token — connect/skip is a local UI morph,
  // no network call.
  const resolveTerminalCredential = useCallback(
    (
      turnId: string,
      action: "connected" | "skip",
      credentialId?: string,
      name?: string,
      continued?: boolean,
    ) => {
      setCredentialResolutions((prev) =>
        withCappedResolution(
          prev,
          turnId,
          action === "connected"
            ? { outcome: "connected", credentialId, name, continued }
            : { outcome: "skipped" },
        ),
      );
    },
    [],
  );
  // A terminal ask is a dead-end otherwise: the turn already ended, so connecting
  // a credential does nothing without a fresh turn. Auto-send one — but only from
  // the tail of an idle conversation (SKY-12384 gating), and only when we know
  // the credential's name to reference.
  const continueAfterTerminalConnect = useCallback(
    (
      turnId: string,
      credentialId: string,
      name: string | undefined,
      canContinue: boolean,
    ) => {
      const shouldContinue =
        canContinue && !isLoading && !isLoadingHistory && Boolean(name);
      resolveTerminalCredential(
        turnId,
        "connected",
        credentialId,
        name,
        shouldContinue,
      );
      if (shouldContinue) {
        pendingTerminalContinuation.current = turnId;
        // Re-attempt: drop any prior stranded flag so the receipt shows again.
        setStrandedTerminalContinuations((prev) => {
          if (!prev.has(turnId)) return prev;
          const next = new Set(prev);
          next.delete(turnId);
          return next;
        });
        // Reference the credential by id: request_policy._explicit_credential_ids resolves a raw cred_
        // id deterministically this turn, avoiding the name-extraction ambiguity a quoted-name reply hits.
        void handleSendRef.current?.(
          `Use the credential ${credentialId} — continue.`,
        );
      }
    },
    [isLoading, isLoadingHistory, resolveTerminalCredential],
  );
  // Auto-continue send failed: undo the optimistic "connected" resolution and
  // mark the ask stranded so its card renders actionable again for a retry.
  const rollbackPendingTerminalContinuation = useCallback(() => {
    const turnId = pendingTerminalContinuation.current;
    if (turnId === null) return;
    pendingTerminalContinuation.current = null;
    setCredentialResolutions((prev) => {
      if (!(turnId in prev)) return prev;
      const next = { ...prev };
      delete next[turnId];
      return next;
    });
    setStrandedTerminalContinuations((prev) => {
      if (prev.has(turnId)) return prev;
      const next = new Set(prev);
      next.add(turnId);
      return next;
    });
  }, []);
  const openCredentialModal = useCallback(
    (
      frame: WorkflowCopilotCredentialRequiredUpdate | null,
      turnId: string,
      isLastMessage = false,
    ) => {
      pendingCredentialConnect.current = { frame, turnId, isLastMessage };
      setCredentialModalOpen(true);
    },
    [],
  );
  const handleCredentialCreated = useCallback(
    (credentialId: string, name?: string) => {
      const ctx = pendingCredentialConnect.current;
      pendingCredentialConnect.current = null;
      setCredentialModalOpen(false);
      // Refresh the picker's cached list so a still-live ask can offer the just-created credential.
      setCredentialsReloadKey((key) => key + 1);
      if (!ctx) return;
      if (ctx.frame) {
        void respondToCredentialPause(
          ctx.frame,
          "connected",
          credentialId,
          name,
        );
      } else {
        continueAfterTerminalConnect(
          ctx.turnId,
          credentialId,
          name,
          ctx.isLastMessage,
        );
      }
    },
    [respondToCredentialPause, continueAfterTerminalConnect],
  );
  // Explore/Draft boundary is unobservable (the LLM writes code with no
  // frames emitted); after DRAFTING_GAP_MS of silence with no pending block
  // run, assume Draft has started. Re-arms per narrative update; the reducer
  // guard makes a stale or double-fired timer a no-op.
  const DRAFTING_GAP_MS = 8000;
  useEffect(() => {
    if (!shouldArmDraftingGapTimer(narrative)) return;
    const wait = Math.max(
      0,
      DRAFTING_GAP_MS - (Date.now() - narrative.lastActivityAtMs!),
    );
    const t = setTimeout(
      () =>
        applyStoredNarrativeEvent({
          type: "client_phase_hint",
          hintedAtMs: Date.now(),
        }),
      wait,
    );
    return () => clearTimeout(t);
  }, [narrative, applyStoredNarrativeEvent]);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const { getSaveData } = useWorkflowHasChangesStore();
  const hasInitializedPosition = useRef(false);
  const hasAutoSentRef = useRef(false);
  const isWaitingForLiveBrowser = shouldWaitForLiveBrowser({
    requiresLiveBrowser,
    isLiveBrowserReady,
  });
  // Reset on initialMessage/action change so a re-arrival of the prop (without a
  // remount) can fire auto-send again.
  useEffect(() => {
    hasAutoSentRef.current = false;
  }, [initialMessage, initialAction?.nonce]);
  // The server rewrites a typed action's message to its own receipt; echoing that same
  // text keeps the row from changing wording when the persisted history reloads.
  const autoSendMessage = initialAction
    ? diagnoseRunReceipt(initialAction.workflowRunId)
    : initialMessage;
  const onInitialMessageConsumedRef = useRef(onInitialMessageConsumed);
  useEffect(() => {
    onInitialMessageConsumedRef.current = onInitialMessageConsumed;
  }, [onInitialMessageConsumed]);
  // Pinned per workflow so dep-change re-fires can't clobber locally-pushed
  // messages, and so auto-send has a synchronous "history loaded" gate.
  const historyLoadedForRef = useRef<string | null>(null);

  const followSignature = useMemo(
    () =>
      computeFollowSignature(
        messages,
        narrative,
        isLoading,
        isLoadingHistory,
        queuedPrompt,
        Boolean(proposedWorkflow),
      ),
    [
      messages,
      narrative,
      isLoading,
      isLoadingHistory,
      queuedPrompt,
      proposedWorkflow,
    ],
  );
  const { scrollRef, isPinned, jumpToLatest, repin } =
    useStickToBottom<HTMLDivElement>(followSignature, { enabled: isOpen });

  const adjustTextareaHeight = useCallback(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;

    if (!textarea.value) {
      textarea.style.height = "40px";
      textarea.style.overflowY = "hidden";
      return;
    }

    textarea.style.height = "auto";
    const newHeight = Math.min(textarea.scrollHeight, 150);
    textarea.style.height = `${newHeight}px`;
    textarea.style.overflowY = newHeight >= 150 ? "auto" : "hidden";
  }, []);

  useEffect(() => {
    adjustTextareaHeight();
  }, [adjustTextareaHeight, inputValue]);

  const resizeObserverRef = useRef<ResizeObserver | null>(null);
  // Bind the observer when the textarea mounts (it can mount late); the width-only guard avoids a resize loop.
  const setTextareaRef = useCallback(
    (node: HTMLTextAreaElement | null) => {
      textareaRef.current = node;
      resizeObserverRef.current?.disconnect();
      resizeObserverRef.current = null;
      if (!node || typeof ResizeObserver === "undefined") return;
      let lastWidth = node.clientWidth;
      const observer = new ResizeObserver(() => {
        if (node.clientWidth !== lastWidth) {
          lastWidth = node.clientWidth;
          adjustTextareaHeight();
        }
      });
      observer.observe(node);
      resizeObserverRef.current = observer;
    },
    [adjustTextareaHeight],
  );

  const {
    isSupported: isSpeechSupported,
    isListening: isSpeechListening,
    isHearingSpeech: isSpeechHearing,
    stop: stopSpeech,
    toggle: toggleSpeech,
    takeAudioBlob: takeSpeechAudioBlob,
  } = useSpeechToTextField({
    value: inputValue,
    onChange: setInputValue,
    // Dictation follows the textarea: it stays live while a prompt is parked,
    // because that text is now editable and replaceable rather than frozen.
    enabled: isOpen,
  });

  const updateQueuedPrompt = useCallback((next: QueuedPrompt | null) => {
    queuedPromptRef.current = next;
    setQueuedPrompt(next);
  }, []);

  const handleNewChat = () => {
    streamingAbortController.current?.abort();
    streamingAbortController.current = null;
    inFlightRef.current = false;
    setIsLoading(false);
    setQuestionInteractions([]);
    setQuestionCancelToken(null);
    stopRecoveryPolls();
    setMessages([]);
    updateQueuedPrompt(null);
    setWorkflowCopilotChatId(null);
    setProposedWorkflow(null);
    setPendingProposalTurnId(null);
    setAutoAccept(false);
    setRejectedTurnIds(new Set());
    setAcceptedTurnIds(new Set());
    setNarrative(EMPTY_NARRATIVE);
    turnSnapshots.current.clear();
    pendingSubmitSnapshot.current = null;
    latestTurnId.current = null;
    connectedAccountChoiceLatch.current = null;
    setConnectedAccountChoicePendingTurnId(null);
    repin();
  };

  const applyHistoryResponse = useCallback(
    (
      data: WorkflowCopilotChatHistoryResponse,
      carryForwardLifecycle = true,
    ) => {
      setQuestionInteractions(data.question_interactions ?? []);
      setQuestionCancelToken(data.pending_question_cancel_token ?? null);
      const historyMessages = data.chat_history.map((message, index) => ({
        id: `${index}-${Date.now()}`,
        sender: message.sender,
        content: message.content,
        timestamp: message.created_at,
        narrative: (() => {
          const hydrated = hydrateHistoryNarrative(
            message.narrative_payload,
            message.turn_outcome,
          );
          if (!hydrated) return undefined;
          // Fall back to the legacy message body when the persisted payload
          // predates terminal-text capture.
          if (!hydrated.terminalMessage && message.content) {
            return {
              ...hydrated,
              terminalMessage: message.content,
              narrativeSummary: hydrated.narrativeSummary ?? message.content,
            };
          }
          return hydrated;
        })(),
      }));
      // A rehydrated turn still owns the run its records name, so the
      // lifecycle hook keeps announcing nothing about it after a reload.
      for (const message of historyMessages) {
        const rehydratedRunId = message.narrative?.turnFacts?.runId;
        if (rehydratedRunId) rememberTurnOwnedRun(rehydratedRunId);
      }
      const restoredPendingProposalTurnId = data.proposed_workflow
        ? getLatestDiffCardTurnId(historyMessages)
        : null;
      latestTurnId.current = restoredPendingProposalTurnId;
      // History never carries run_lifecycle lines (local-only); carry them
      // forward only for the mount-race caller, not an explicit chat switch.
      setMessages((prev) => [
        ...historyMessages,
        ...(carryForwardLifecycle
          ? prev.filter((message) => message.kind === "run_lifecycle")
          : []),
      ]);
      setWorkflowCopilotChatId(data.workflow_copilot_chat_id);
      setProposedWorkflow(data.proposed_workflow ?? null);
      setPendingProposalTurnId(
        data.proposed_workflow ? restoredPendingProposalTurnId : null,
      );
      setAutoAccept(data.auto_accept ?? false);
    },
    // Only stable state setters and refs are referenced, so the callback never needs to change.
    [rememberTurnOwnedRun],
  );

  const stopRecoveryPolls = useCallback(() => {
    // Bumping the generation also discards responses already in flight, which
    // clearTimeout alone cannot cancel.
    recoveryGeneration.current += 1;
    recoveryPolls.current.forEach((stop) => stop());
    recoveryPolls.current.clear();
  }, []);

  const startRecoveryPoll = useCallback(
    (chatId: string | null, turnId: string) => {
      if (!workflowPermanentId) {
        return;
      }
      const generation = recoveryGeneration.current;
      const deadline = Date.now() + RECOVERY_POLL_BUDGET_MS;
      let timer: ReturnType<typeof setTimeout> | null = null;
      let step = 0;
      let appliedContent: string | null = null;
      // Latching an id below makes later reads consistent, but an id resolved
      // from "the workflow's latest chat" is not proof the chat is this turn's,
      // so what gates the untagged fallback is how the poll started.
      const scopedToKnownChat = chatId !== null;
      let untaggedBaseline: number | null = null;
      let inFlight: AbortController | null = null;
      let deadlineTimer: ReturnType<typeof setTimeout> | null = null;
      let consecutiveFailures = 0;
      let supersedeReadsLeft = RECOVERY_POLL_SUPERSEDE_READS;
      let stopped = false;

      const clearTimer = () => {
        if (timer !== null) {
          clearTimeout(timer);
          timer = null;
        }
        // Without this a hung request outlives the budget and unmount cleanup,
        // which clear only the timer.
        inFlight?.abort();
        inFlight = null;
      };
      function finish() {
        stopped = true;
        clearTimer();
        if (deadlineTimer !== null) {
          clearTimeout(deadlineTimer);
          deadlineTimer = null;
        }
        if (recoveryPolls.current.get(turnId) === finish) {
          recoveryPolls.current.delete(turnId);
        }
      }

      // Ending without the turn's row leaves a notice describing work that has
      // stopped, so it reverts to the plain failure it replaced.
      function giveUp() {
        finish();
        setMessages((prev) =>
          prev.map((message) =>
            message.content === RECOVERY_IN_PROGRESS_MESSAGE
              ? { ...message, content: SEND_FAILED_MESSAGE }
              : message,
          ),
        );
      }

      recoveryPolls.current.get(turnId)?.();
      recoveryPolls.current.set(turnId, finish);
      // A read that never returns leaves schedule() unreached, so the budget
      // needs a timer of its own or the deadline can never fire.
      deadlineTimer = setTimeout(giveUp, RECOVERY_POLL_BUDGET_MS);

      function schedule() {
        clearTimer();
        // finish() aborts the in-flight read, which surfaces in tick's catch;
        // without this the rejection would reschedule a stopped poll.
        if (stopped || Date.now() >= deadline) {
          finish();
          return;
        }
        const delay = RECOVERY_POLL_DELAYS_MS[step] ?? RECOVERY_POLL_STEADY_MS;
        step += 1;
        timer = setTimeout(() => {
          timer = null;
          void tick();
        }, delay);
      }

      async function tick() {
        if (recoveryGeneration.current !== generation) {
          finish();
          return;
        }
        const sendEpochBeforeRead = sendEpoch.current;
        try {
          const client = await getClient(credentialGetter, "sans-api-v1");
          const controller = new AbortController();
          inFlight = controller;
          const response = await client.get<WorkflowCopilotChatHistoryResponse>(
            "/workflow/copilot/chat-history",
            {
              params: chatId
                ? { workflow_copilot_chat_id: chatId }
                : { workflow_permanent_id: workflowPermanentId },
              signal: controller.signal,
            },
          );
          if (inFlight === controller) {
            inFlight = null;
          }
          // Without a chat id the read resolves "the workflow's latest chat",
          // which another tab can move by creating a newer one. Latching the
          // first id keeps every later read on the chat this turn recovered in.
          if (chatId === null && response.data.workflow_copilot_chat_id) {
            chatId = response.data.workflow_copilot_chat_id;
          }
          if (recoveryGeneration.current !== generation) {
            finish();
            return;
          }
          const pendingQuestion = response.data.question_interactions?.find(
            (item) => item.turn_id === turnId && item.status === "pending",
          );
          if (pendingQuestion) {
            setWorkflowCopilotChatId(chatId);
            workflowCopilotChatIdRef.current = chatId;
            setQuestionInteractions(response.data.question_interactions ?? []);
            setQuestionCancelToken(
              response.data.pending_question_cancel_token ?? null,
            );
            setIsLoading(false);
            finish();
            return;
          }
          if (untaggedBaseline === null) {
            // Polling by workflow_permanent_id can resolve to another tab's
            // chat, so an untagged row there is not ours to adopt. The last row
            // is excluded so a reply that landed before this first read can
            // still clear the baseline it would otherwise have set.
            untaggedBaseline = !scopedToKnownChat
              ? Number.POSITIVE_INFINITY
              : response.data.chat_history
                  .slice(0, -1)
                  .reduce((newest, message) => {
                    const stamp = parseServerStamp(message.created_at);
                    return stamp > newest ? stamp : newest;
                  }, Number.NEGATIVE_INFINITY);
          }
          const row = findRecoveredRow(
            response.data.chat_history,
            turnId,
            untaggedBaseline,
          );
          if (row) {
            // Applying history while a later turn streams would wipe its
            // optimistic rows, so leave the row for a tick after that send.
            // The epoch covers a send that began and ended within this read,
            // which leaves inFlightRef false at both ends of it.
            if (
              inFlightRef.current ||
              sendEpoch.current !== sendEpochBeforeRead
            ) {
              schedule();
              return;
            }
            if (row.content !== appliedContent) {
              appliedContent = row.content;
              applyHistoryResponse(response.data);
              setIsLoading(false);
            }
            // An interrupted row is replaced by the real reply if the turn
            // later finishes, so only a finished row ends the poll.
            if (
              row.turn_outcome?.terminal_reason !== INTERRUPTED_TERMINAL_REASON
            ) {
              void reconcileCanonicalWorkflowRef.current?.();
              finish();
              return;
            }
            // A turn cancelled operationally (a worker drain) writes this row
            // and never finishes, so waiting out the budget would have every
            // open chat polling in lockstep through a deploy. The user already
            // has a truthful row; only a few reads are spent on a supersede.
            supersedeReadsLeft -= 1;
            if (supersedeReadsLeft <= 0) {
              finish();
              return;
            }
          }
        } catch (error) {
          console.warn("Copilot recovery poll failed:", error);
          if (recoveryGeneration.current !== generation) {
            finish();
            return;
          }
          // The usual cause of a severed stream is the client losing the
          // network, and then every read fails too. Rescheduling through that
          // holds the notice for the whole budget and withholds the error the
          // user used to get at once, so a run of failures ends the poll.
          consecutiveFailures += 1;
          if (consecutiveFailures >= RECOVERY_POLL_FAILURE_CEILING) {
            giveUp();
            return;
          }
          schedule();
          return;
        }
        consecutiveFailures = 0;
        schedule();
      }

      schedule();
    },
    [applyHistoryResponse, credentialGetter, workflowPermanentId],
  );

  const loadChatInPlace = useCallback(
    async (chatId: string) => {
      if (!workflowPermanentId) return;
      if (workflowCopilotChatIdRef.current !== chatId) {
        streamingAbortController.current?.abort();
        streamingAbortController.current = null;
        inFlightRef.current = false;
        setIsLoading(false);
      }
      stopRecoveryPolls();
      setIsLoadingHistory(true);
      updateQueuedPrompt(null);
      setRejectedTurnIds(new Set());
      setAcceptedTurnIds(new Set());
      setNarrative(EMPTY_NARRATIVE);
      turnSnapshots.current.clear();
      pendingSubmitSnapshot.current = null;
      latestTurnId.current = null;
      repin();
      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        const response = await client.get<WorkflowCopilotChatHistoryResponse>(
          "/workflow/copilot/chat-history",
          {
            params: {
              workflow_permanent_id: workflowPermanentId,
              workflow_copilot_chat_id: chatId,
            },
          },
        );
        applyHistoryResponse(response.data, false);
        // Mark history loaded for this workflow so the mount effect won't reload
        // the latest chat over the one the user just selected.
        historyLoadedForRef.current = workflowPermanentId;
      } catch (error) {
        console.error("Failed to load chat:", error);
        toast({ title: "Failed to load chat", variant: "destructive" });
      } finally {
        setIsLoadingHistory(false);
      }
    },
    [
      credentialGetter,
      workflowPermanentId,
      applyHistoryResponse,
      stopRecoveryPolls,
      updateQueuedPrompt,
      repin,
    ],
  );

  const handleSelectHistoryChat = useCallback(
    (chat: WorkflowCopilotChatSummary) => {
      if (chat.workflow_copilot_chat_id === workflowCopilotChatIdRef.current) {
        return;
      }
      void loadChatInPlace(chat.workflow_copilot_chat_id);
    },
    [loadChatInPlace],
  );

  // Hand the studio's Copilot pane header its History/New-chat controls.
  // Stable wrappers over refs keep the registration limited to value changes.
  const headerHandlersRef = useRef({ handleSelectHistoryChat, handleNewChat });
  headerHandlersRef.current = { handleSelectHistoryChat, handleNewChat };
  const headerControlsDisabled = isLoading || isLoadingHistory;
  useEffect(() => {
    if (!docked) {
      return;
    }
    const store = useCopilotHeaderStore.getState();
    store.setControls({
      workflowPermanentId,
      currentChatId: workflowCopilotChatId,
      onSelectChat: (chat) =>
        headerHandlersRef.current.handleSelectHistoryChat(chat),
      onNewChat: () => headerHandlersRef.current.handleNewChat(),
      disabled: headerControlsDisabled,
    });
    return () => store.setControls(null);
  }, [
    docked,
    workflowPermanentId,
    workflowCopilotChatId,
    headerControlsDisabled,
  ]);

  const applyWorkflowUpdate = useCallback(
    (
      workflow: WorkflowApiResponse,
      options?: WorkflowUpdateOptions,
    ): boolean => {
      if (!onWorkflowUpdate) {
        return true;
      }
      try {
        onWorkflowUpdate(workflow, options);
        return true;
      } catch (updateError) {
        console.error("Failed to update workflow:", updateError);
        toast({
          title: "Update failed",
          description: "Failed to apply agent changes. Please try again.",
          variant: "destructive",
        });
        return false;
      }
    },
    [onWorkflowUpdate],
  );

  // A turn that auto-committed a build applies it from the terminal frame. A
  // recovered turn never had that frame, so the editor can still hold the graph
  // from before the drop and a later save would write it back over the commit.
  // Reading canonical is the same thing the reload used to do.
  const reconcileCanonicalWorkflow = useCallback(async () => {
    if (!workflowPermanentId) {
      return;
    }
    // Unsaved local edits outrank a stale graph: overwriting them would lose
    // work the user can see, which is worse than the staleness being fixed.
    if (useWorkflowHasChangesStore.getState().hasChanges) {
      return;
    }
    try {
      const client = await getClient(credentialGetter);
      const response = await client.get<WorkflowApiResponse>(
        `/workflows/${workflowPermanentId}`,
      );
      applyWorkflowUpdate(response.data, { persisted: true, applied: true });
    } catch (error) {
      console.warn("Failed to re-read the workflow after recovery:", error);
    }
  }, [applyWorkflowUpdate, credentialGetter, workflowPermanentId]);
  reconcileCanonicalWorkflowRef.current = reconcileCanonicalWorkflow;

  // Records the accepted turn (for the "Applied changes" relabel) before
  // clearing the pending-gate handle, shared by all three accept outcomes.
  const markProposalAccepted = () => {
    if (pendingProposalTurnId) {
      setAcceptedTurnIds((prev) => new Set(prev).add(pendingProposalTurnId));
    }
    setPendingProposalTurnId(null);
  };

  const handleAcceptWorkflow = async (
    workflow: WorkflowApiResponse,
    alwaysAccept: boolean = false,
  ) => {
    let chatId = workflowCopilotChatIdRef.current?.trim() || null;
    if (!chatId) {
      try {
        chatId = await fetchLatestChatId();
      } catch (resolveError) {
        console.error(
          "Failed to resolve chat ID before applying proposal:",
          resolveError,
        );
      }
    }

    if (!chatId) {
      // No chat id: apply locally and best-effort clear the server proposal so reload doesn't resurrect it.
      if (!applyWorkflowUpdate(workflow, { applied: true })) {
        return;
      }
      markProposalAccepted();
      setProposedWorkflow(null);
      if (alwaysAccept) {
        setAutoAccept(true);
      }
      void clearProposedWorkflow(alwaysAccept);
      return;
    }

    try {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response = await client.post<WorkflowApiResponse>(
        "/workflow/copilot/apply-proposed-workflow",
        {
          workflow_copilot_chat_id: chatId,
          auto_accept: alwaysAccept,
        } as WorkflowCopilotApplyProposedWorkflowRequest,
      );
      // persisted=true loads as clean baseline; without it, Save would create a duplicate version.
      if (
        !applyWorkflowUpdate(response.data, { persisted: true, applied: true })
      ) {
        return;
      }
      markProposalAccepted();
      setProposedWorkflow(null);
      if (alwaysAccept) {
        setAutoAccept(true);
      }
    } catch (applyError) {
      // Atomic accept can fail if the server-side proposal is missing
      // _copilot_yaml (SKY-9310 — V1 path didn't stash it). Fall back to the
      // pre-#10568 client-side apply so users aren't blocked while a backend
      // deploy catches up. Logged so we can still spot regressions.
      console.error(
        "Atomic apply failed; falling back to client-side apply:",
        applyError,
      );
      if (!applyWorkflowUpdate(workflow, { applied: true })) {
        toast({
          title: "Accept failed",
          description: "Could not apply the proposed agent. Please try again.",
          variant: "destructive",
        });
        return;
      }
      markProposalAccepted();
      setProposedWorkflow(null);
      if (alwaysAccept) {
        setAutoAccept(true);
      }
      void clearProposedWorkflow(alwaysAccept);
    }
  };

  const handleRejectWorkflow = () => {
    // The staged proposal was rendered onto the canvas mid-turn (via
    // WORKFLOW_DRAFT). Reject must revert the canvas to the pre-submit
    // canvas state captured client-side at submit time.
    const turnId =
      pendingProposalTurnId ??
      latestTurnId.current ??
      getLatestDiffCardTurnId(messages);
    const entry = turnId ? turnSnapshots.current.get(turnId) : null;
    if (entry?.snapshot) {
      applyWorkflowUpdate(entry.snapshot);
    }
    if (turnId) {
      setRejectedTurnIds((prev) => new Set(prev).add(turnId));
    }
    setProposedWorkflow(null);
    setPendingProposalTurnId(null);
    void clearProposedWorkflow(false);
  };

  const getErrorStatus = (error: unknown): number | undefined => {
    const response = (error as { response?: { status?: number } })?.response;
    return response?.status;
  };

  const fetchLatestChatId = async (): Promise<string | null> => {
    if (!workflowPermanentId) {
      return null;
    }
    const client = await getClient(credentialGetter, "sans-api-v1");
    const response = await client.get<WorkflowCopilotChatHistoryResponse>(
      "/workflow/copilot/chat-history",
      {
        params: { workflow_permanent_id: workflowPermanentId },
      },
    );
    const latestChatId = response.data.workflow_copilot_chat_id ?? null;
    setWorkflowCopilotChatId(latestChatId);
    return latestChatId;
  };

  const uploadDictationAudio = useCallback(
    async (audioBlob: Blob): Promise<WorkflowCopilotAudioUploadResponse> => {
      if (!workflowPermanentId) {
        throw new Error("Missing workflow permanent ID for audio upload.");
      }

      const client = await getClient(credentialGetter, "sans-api-v1");
      const formData = new FormData();
      formData.append("workflow_permanent_id", workflowPermanentId);
      const chatId = workflowCopilotChatIdRef.current?.trim();
      if (chatId) {
        formData.append("workflow_copilot_chat_id", chatId);
      }
      formData.append("file", audioBlob, `dictation-${Date.now()}.webm`);

      const response = await client.post<WorkflowCopilotAudioUploadResponse>(
        "/workflow/copilot/chat-audio",
        formData,
        {
          headers: {
            "Content-Type": "multipart/form-data",
          },
        },
      );
      setWorkflowCopilotChatId(response.data.workflow_copilot_chat_id);
      workflowCopilotChatIdRef.current = response.data.workflow_copilot_chat_id;
      return response.data;
    },
    [credentialGetter, workflowPermanentId],
  );

  // A follow-up turn that ends without a new draft no longer nulls a bypassed
  // proposal client-side; re-fetch the chat row instead, since the
  // backend (keep_pending_proposal) may have kept it alive server-side.
  // useCallback-stable: handleSend depends on it and is itself a dependency
  // of other effects, so a churning identity here would cascade into them.
  const resyncProposalFromChatRow = useCallback(async (): Promise<void> => {
    const chatId = workflowCopilotChatIdRef.current?.trim();
    if (!chatId) {
      return;
    }
    try {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response = await client.get<WorkflowCopilotChatHistoryResponse>(
        "/workflow/copilot/chat-history",
        { params: { workflow_copilot_chat_id: chatId } },
      );
      const nextProposal = response.data.proposed_workflow ?? null;
      setProposedWorkflow(nextProposal);
      if (!nextProposal) {
        setPendingProposalTurnId(null);
      }
    } catch (error) {
      console.error("Failed to resync pending proposal:", error);
    }
  }, [credentialGetter]);

  const clearProposedWorkflow = async (autoAcceptValue: boolean) => {
    const clearProposalByChatId = async (chatId: string) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      await client.post<WorkflowCopilotClearProposedWorkflowRequest>(
        "/workflow/copilot/clear-proposed-workflow",
        {
          workflow_copilot_chat_id: chatId,
          auto_accept: autoAcceptValue,
        } as WorkflowCopilotClearProposedWorkflowRequest,
      );
    };

    let chatId = workflowCopilotChatIdRef.current?.trim() || null;
    if (!chatId) {
      try {
        chatId = await fetchLatestChatId();
      } catch (resolveError) {
        console.error(
          "Failed to resolve chat ID before clearing proposal:",
          resolveError,
        );
        return;
      }
    }

    if (!chatId) {
      return;
    }

    try {
      await clearProposalByChatId(chatId);
    } catch (error) {
      const status = getErrorStatus(error);
      if (status === 404) {
        try {
          const refreshedChatId = await fetchLatestChatId();
          if (refreshedChatId && refreshedChatId !== chatId) {
            await clearProposalByChatId(refreshedChatId);
            return;
          }
        } catch (retryError) {
          console.error("Retry to clear proposed workflow failed:", retryError);
        }
      }
      console.error("Failed to clear proposed workflow:", error);
      toast({
        title: "Copilot update failed",
        description: autoAcceptValue
          ? "Agent was applied, but auto-accept did not update."
          : "Failed to clear copilot proposal. Please try again.",
        variant: "destructive",
      });
    }
  };

  const handleReviewWorkflow = (workflow: WorkflowApiResponse) => {
    onReviewWorkflow?.(workflow, () => {
      setProposedWorkflow(null);
      setPendingProposalTurnId(null);
    });
  };

  useEffect(() => {
    if (onMessageCountChange) {
      onMessageCountChange(messages.length);
    }
  }, [messages.length, onMessageCountChange]);

  useEffect(() => {
    if (!workflowPermanentId) {
      stopRecoveryPolls();
      setMessages([]);
      updateQueuedPrompt(null);
      setWorkflowCopilotChatId(null);
      setProposedWorkflow(null);
      setPendingProposalTurnId(null);
      setAutoAccept(false);
      setNarrative(EMPTY_NARRATIVE);
      historyLoadedForRef.current = null;
      return;
    }

    if (historyLoadedForRef.current === workflowPermanentId) {
      return;
    }
    // Reached only when this workflow's transcript is about to replace another's,
    // so a poll armed against the outgoing chat must not apply history here.
    stopRecoveryPolls();

    let isMounted = true;

    const fetchHistory = async () => {
      setIsLoadingHistory(true);
      repin();
      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        const response = await client.get<WorkflowCopilotChatHistoryResponse>(
          "/workflow/copilot/chat-history",
          {
            params: { workflow_permanent_id: workflowPermanentId },
          },
        );

        if (!isMounted) return;

        applyHistoryResponse(response.data);
        historyLoadedForRef.current = workflowPermanentId;
      } catch (error) {
        console.error("Failed to load chat history:", error);
      } finally {
        if (isMounted) {
          setIsLoadingHistory(false);
        }
      }
    };

    fetchHistory();

    return () => {
      isMounted = false;
    };
  }, [
    credentialGetter,
    repin,
    stopRecoveryPolls,
    updateQueuedPrompt,
    workflowPermanentId,
    applyHistoryResponse,
  ]);

  // Set by a block's "Generate" arm step so the next send scopes regeneration to that block.
  const blockBuildTargetLabelRef = useRef<string | null>(null);
  // Set by a product affordance so the next send posts that typed action instead of prose.
  const productActionRef = useRef<ArmedProductAction | null>(null);
  const echoSenderForArmedAction = (): WorkflowCopilotChatSender =>
    productActionRef.current?.action === "diagnose_run" ? "product" : "user";
  // True only while a block-build turn is actually in flight (not a turn it queued behind).
  const blockGenInFlightRef = useRef(false);

  // Disposal path that hands the queued text back to the composer as an
  // editable draft; the drain effect's duplicate drop is the one path that
  // discards instead. Reads the synchronous ref, not state, so a stop can
  // clear the queue before isLoading flips and the drain effect runs.
  const restoreQueuedPromptToComposer = useCallback(() => {
    const queued = queuedPromptRef.current;
    if (!queued) {
      return;
    }

    // A diagnose action's queued text is the server's receipt, not words the user wrote, so
    // handing it back as an editable draft would repost it as a user message.
    const wasDiagnoseAction =
      productActionRef.current?.action === "diagnose_run";

    updateQueuedPrompt(null);
    // Drop the queued block-build target and end-to-end action so neither leaks into the next
    // message. Deferring paths (queue_working / queue_live_browser) keep the action armed on
    // purpose; only abandoning the message disarms it.
    blockBuildTargetLabelRef.current = null;
    productActionRef.current = null;
    setMessages((prev) => prev.filter((message) => message.id !== queued.id));
    // Text already in the composer is the newer intent — the user was part way
    // through replacing the queued message — so it wins over what comes back.
    if (!wasDiagnoseAction) {
      setInputValue((current) => (current.trim() ? current : queued.content));
    }
    window.requestAnimationFrame(() => {
      textareaRef.current?.focus();
      adjustTextareaHeight();
    });
  }, [adjustTextareaHeight, updateQueuedPrompt]);

  const cancelSend = useCallback(
    async (
      source: WorkflowCopilotCancelSource,
      // A stop control must not fire before the turn is observably running, or a fast
      // double-click on Send is "send, then cancel". The deliberate gestures -- Escape,
      // and a block's own cancel -- are exempt, so a turn that hangs before its first
      // frame is still stoppable rather than leaving every path dead for that window.
      { requireArmed = true }: { requireArmed?: boolean } = {},
    ) => {
      // A stop must never let a queued message auto-fire. Hand its text back to
      // the composer synchronously, before isLoading flips and the drain effect
      // would otherwise send it as a fresh turn.
      restoreQueuedPromptToComposer();

      if (requireArmed && !turnObservablyRunningRef.current) return;

      // Capture upfront so the 15s timer below can't latch onto a next turn's controller.
      const controllerAtCancel = streamingAbortController.current;
      if (!controllerAtCancel) return;

      const cancelToken = pendingCancelToken.current;
      pendingCancelToken.current = null;
      // After the token check, not before: a turn that already finished has no
      // token, and claiming "Stopping…" for a cancel that never goes out would
      // rely on the isLoading reset as its only way back.
      if (!cancelToken) return;

      setIsStopping(true);

      cancelInFlightController.current = controllerAtCancel;

      // The backend is still running when this fires, so it cannot report what the
      // turn recorded; the persisted row stays authoritative on reload.
      const appendStopUnconfirmedNotice = (content: string) => {
        setMessages((prev) => [
          ...prev,
          {
            id: `${Date.now()}-cancel`,
            sender: "ai",
            content,
            timestamp: new Date().toISOString(),
            kind: "status_notice",
          },
        ]);
        // Otherwise the working bubble freezes mid-state next to the notice.
        setNarrative(EMPTY_NARRATIVE);
      };

      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        await client.post<void>("/workflow/copilot/cancel", {
          cancel_token: cancelToken,
          source,
        } as WorkflowCopilotCancelRequest);
        // Safety net: if the SSE channel never resolves, surface a fallback
        // bubble and abort so handleSend's finally clears "Cancelling...".
        if (cancelSafetyTimer.current !== null) {
          clearTimeout(cancelSafetyTimer.current);
        }
        cancelSafetyTimer.current = setTimeout(() => {
          cancelSafetyTimer.current = null;
          if (streamingAbortController.current !== controllerAtCancel) return;
          appendStopUnconfirmedNotice(STOP_UNCONFIRMED_NOTICE);
          controllerAtCancel.abort();
        }, 15_000);
      } catch (error) {
        // 503 (Redis disabled) or network failure: client-side abort still
        // gives the user immediate feedback; the backend will run to
        // completion in that environment. Log so we can spot it in dev.
        console.warn("Workflow copilot cancel POST failed", error);
        controllerAtCancel.abort();
        appendStopUnconfirmedNotice(STOP_NOT_SENT_NOTICE);
      }
    },
    [credentialGetter, restoreQueuedPromptToComposer],
  );

  // The turn ending is the one signal that a stop actually landed — it covers
  // the normal path, the 15s safety-timer abort, and the failed-POST abort.
  useEffect(() => {
    if (!isLoading) {
      setIsStopping(false);
    }
  }, [isLoading]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || !isOpen) {
        return;
      }
      // Dismissing an IME conversion candidate fires Escape with isComposing
      // set; the user is editing text, not stopping their turn.
      if (event.isComposing) {
        return;
      }
      if (queuedPrompt) {
        restoreQueuedPromptToComposer();
        return;
      }
      if (isLoading) {
        cancelSend("escape_key", { requireArmed: false });
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [
    restoreQueuedPromptToComposer,
    cancelSend,
    isLoading,
    isOpen,
    queuedPrompt,
  ]);

  const [isSubmittingQuestion, setIsSubmittingQuestion] = useState(false);
  const submittingQuestion = useRef(false);
  const handleQuestionAnswer = useCallback(
    async (interaction: QuestionInteraction, response: QuestionResponse) => {
      if (!workflowCopilotChatId || submittingQuestion.current) return false;
      submittingQuestion.current = true;
      setIsSubmittingQuestion(true);
      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        const accepted = await client.post<QuestionInteraction>(
          "/workflow/copilot/question-response",
          {
            workflow_copilot_chat_id: workflowCopilotChatId,
            interaction_id: interaction.interaction_id,
            ...response,
          },
        );
        if (workflowCopilotChatIdRef.current !== workflowCopilotChatId)
          return true;
        setQuestionInteractions((current) =>
          current.map((item) =>
            item.interaction_id === interaction.interaction_id
              ? accepted.data
              : item,
          ),
        );
        setIsLoading(true);
        if (!streamingAbortController.current)
          startRecoveryPoll(workflowCopilotChatId, interaction.turn_id);
        return true;
      } catch {
        toast({
          title: "Could not send your response",
          description: "Please check the question and try again.",
          variant: "destructive",
        });
        if (workflowCopilotChatIdRef.current === workflowCopilotChatId)
          void loadChatInPlace(workflowCopilotChatId);
        return false;
      } finally {
        submittingQuestion.current = false;
        setIsSubmittingQuestion(false);
      }
    },
    [
      credentialGetter,
      workflowCopilotChatId,
      loadChatInPlace,
      startRecoveryPoll,
    ],
  );

  const hasPendingQuestion = questionInteractions.some(
    (item) => item.status === "pending",
  );
  useEffect(() => {
    if (!hasPendingQuestion || !workflowCopilotChatId) return;
    let disposed = false;
    const refresh = async () => {
      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        const { data } = await client.get<WorkflowCopilotChatHistoryResponse>(
          "/workflow/copilot/chat-history",
          {
            params: { workflow_copilot_chat_id: workflowCopilotChatId },
          },
        );
        if (disposed) return;
        const interactions = data.question_interactions ?? [];
        setQuestionInteractions(interactions);
        setQuestionCancelToken(data.pending_question_cancel_token ?? null);
        if (
          !interactions.some((item) => item.status === "pending") &&
          !streamingAbortController.current
        ) {
          const resolved = [...interactions]
            .reverse()
            .find((item) => item.status === "resolved");
          if (resolved) {
            setIsLoading(true);
            startRecoveryPoll(workflowCopilotChatId, resolved.turn_id);
          } else {
            setIsLoading(false);
            inFlightRef.current = false;
          }
        }
      } catch {
        /* A temporary disconnect leaves the saved card intact until reconnect. */
      }
    };
    const timer = setInterval(() => void refresh(), 5000);
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  }, [
    hasPendingQuestion,
    workflowCopilotChatId,
    credentialGetter,
    startRecoveryPoll,
  ]);

  const handleSend = useCallback(
    async (messageOverride?: string, options: SendOptions = {}) => {
      const candidate = messageOverride ?? inputValue;
      const pendingQuestion = questionInteractions.find(
        (item) => item.status === "pending",
      );
      if (pendingQuestion && candidate !== "") {
        if (await handleQuestionAnswer(pendingQuestion, { text: candidate }))
          setInputValue("");
        return;
      }
      const isDrain = Boolean(options.queuedMessageId);
      const action = resolveSendAction({
        inFlight: inFlightRef.current,
        hasQueuedPrompt: Boolean(queuedPromptRef.current),
        requiresLiveBrowser,
        isLiveBrowserReady,
        candidate,
        isDrain,
        skipQueue: Boolean(options.skipQueue),
      });
      if (action === "noop") {
        // Nothing was sent, so the arm must not survive onto whatever the user types next.
        productActionRef.current = null;
        return;
      }
      if (!workflowPermanentId) {
        productActionRef.current = null;
        toast({
          title: "Missing agent",
          description: "Agent permanent ID is required to chat.",
          variant: "destructive",
        });
        return;
      }

      let messageAudioBlob = options.audioBlob ?? null;
      if (!messageAudioBlob && messageOverride === undefined) {
        if (isSpeechListening) {
          messageAudioBlob = await stopSpeech();
        }
        messageAudioBlob = messageAudioBlob ?? takeSpeechAudioBlob();
      }

      if (action === "replace_queued") {
        const queued = queuedPromptRef.current;
        if (!queued) {
          return;
        }
        // New text: the block-build scope and the end-to-end action belonged to the message being
        // replaced. Carrying the action over would run the whole workflow for real on text the user
        // wrote to say something else.
        blockBuildTargetLabelRef.current = null;
        productActionRef.current = null;
        updateQueuedPrompt({
          ...queued,
          content: candidate,
          audioBlob: messageAudioBlob,
          idempotencyKey: options.idempotencyKey,
          selectedConnectedAccountId: options.selectedConnectedAccountId,
        });
        setMessages((prev) =>
          prev.map((message) =>
            message.id === queued.id
              ? { ...message, sender: "user", content: candidate }
              : message,
          ),
        );
        if (messageOverride === undefined) {
          setInputValue("");
        }
        return;
      }

      if (action === "queue_working" || action === "queue_live_browser") {
        const reason: QueuedPromptReason =
          action === "queue_working" ? "working" : "live_browser";
        const queuedId = options.queuedMessageId ?? crypto.randomUUID();
        updateQueuedPrompt({
          id: queuedId,
          content: candidate,
          reason,
          audioBlob: messageAudioBlob,
          idempotencyKey: options.idempotencyKey,
          selectedConnectedAccountId: options.selectedConnectedAccountId,
        });
        // First queue adds the user bubble; a re-queue (a working drain that
        // then had to wait for the browser) reuses the existing bubble.
        if (!options.queuedMessageId) {
          setMessages((prev) => [
            ...prev,
            {
              id: queuedId,
              sender: echoSenderForArmedAction(),
              content: candidate,
            },
          ]);
        }
        if (messageOverride === undefined) {
          setInputValue("");
        }
        if (!options.queuedMessageId) {
          toast(
            reason === "working"
              ? {
                  title: "Message queued",
                  description:
                    "Copilot is finishing the current turn — it will send next.",
                }
              : {
                  title: "Prompt queued",
                  description:
                    "Copilot will start once the live browser connects.",
                },
          );
        }
        return;
      }

      const userMessageId = options.queuedMessageId ?? Date.now().toString();
      const userMessage: ChatMessage = {
        id: userMessageId,
        sender: echoSenderForArmedAction(),
        content: candidate,
      };

      const cancelToken = crypto.randomUUID();
      pendingCancelToken.current = cancelToken;
      buildFollowEngaged.current = true;
      lastFollowedLabelRef.current = null;

      pendingMessageId.current = userMessageId;
      if (!options.queuedMessageId) {
        setMessages((prev) => [...prev, userMessage]);
      }
      const messageContent = candidate;
      if (messageOverride === undefined && !options.queuedMessageId) {
        setInputValue("");
      }
      setIsLoading(true);
      inFlightRef.current = true;
      sendEpoch.current += 1;
      // Stamped here, before the awaits below consume messageAudioBlob
      // and blockBuildTargetLabelRef.
      lastTurnRef.current = {
        content: candidate,
        workflowPermanentId,
        hadAudio: messageAudioBlob !== null,
        hadBlockTarget: blockBuildTargetLabelRef.current !== null,
        browserSessionId: liveBrowserSessionId ?? null,
        codeBlock: codeBlockModeEnabled ? codeBlockRequestOverride : false,
        completedNormally: false,
      };
      // Clear the prior turn's lingering narrative so the instant-ack placeholder's
      // turnId===null gate holds on every send, and the first frame hands off cleanly.
      // Reset the ref too (it lags setNarrative by a passive effect) so a late
      // prior-turn recorded-actions fetch can't rebase onto the stale narrative.
      narrativeRef.current = EMPTY_NARRATIVE;
      setNarrative(EMPTY_NARRATIVE);

      const abortController = new AbortController();
      streamingAbortController.current?.abort();
      streamingAbortController.current = abortController;
      // A chat switch or New chat during the stream bumps this, which is how a
      // turn whose chat the user left is kept from arming a recovery re-read.
      let sendGeneration = recoveryGeneration.current;
      let streamTurnId: string | null = null;
      let streamChatId: string | null = null;
      let sawTerminalFrame = false;
      const shouldArmRecovery = () =>
        streamTurnId !== null &&
        !sawTerminalFrame &&
        !abortController.signal.aborted &&
        cancelInFlightController.current !== abortController &&
        recoveryGeneration.current === sendGeneration;

      setStopArmed(false);
      if (stopArmTimer.current !== null) {
        clearTimeout(stopArmTimer.current);
      }
      stopArmTimer.current = setTimeout(() => {
        stopArmTimer.current = null;
        // A queued prompt can drain into a next turn before this fires; only the
        // turn that scheduled it may arm.
        if (streamingAbortController.current !== abortController) return;
        setStopArmed(true);
      }, STOP_ARM_DOUBLE_TAP_MS);

      try {
        const saveData = getSaveData();
        const workflowId = saveData?.workflow.workflow_id;
        let workflowYaml = "";
        let chatIdForRequest = workflowCopilotChatId;
        let audioArtifactId: string | null = null;

        if (!workflowId) {
          productActionRef.current = null;
          toast({
            title: "Missing agent",
            description: "Agent ID is required to chat.",
            variant: "destructive",
          });
          return;
        }

        if (saveData) {
          const extraHttpHeaders: Record<string, string> = {};
          if (saveData.settings.extraHttpHeaders) {
            try {
              const parsedHeaders = JSON.parse(
                saveData.settings.extraHttpHeaders,
              );
              if (
                parsedHeaders &&
                typeof parsedHeaders === "object" &&
                !Array.isArray(parsedHeaders)
              ) {
                for (const [key, value] of Object.entries(parsedHeaders)) {
                  if (key && typeof key === "string") {
                    extraHttpHeaders[key] = String(value);
                  }
                }
              }
            } catch (error) {
              console.error("Error parsing extra HTTP headers:", error);
            }
          }

          const scriptCacheKey = saveData.settings.scriptCacheKey ?? "";
          const normalizedKey =
            scriptCacheKey === ""
              ? "default"
              : saveData.settings.scriptCacheKey;

          const requestBody: WorkflowCreateYAMLRequest = {
            title: saveData.title,
            description: saveData.workflow.description,
            proxy_location: saveData.settings.proxyLocation,
            webhook_callback_url: saveData.settings.webhookCallbackUrl,
            persist_browser_session: saveData.settings.persistBrowserSession,
            reuse_browser_session: saveData.settings.reuseBrowserSession,
            pin_saved_session_ip: saveData.settings.pinSavedSessionIp,
            browser_profile_id: saveData.settings.browserProfileId,
            browser_profile_key: saveData.settings.browserProfileKey,
            model: saveData.settings.model,
            max_screenshot_scrolls: saveData.settings.maxScreenshotScrolls,
            max_elapsed_time_minutes:
              saveData.settings.maxElapsedTimeMinutes ?? null,
            totp_verification_url: saveData.workflow.totp_verification_url,
            extra_http_headers: extraHttpHeaders,
            run_with: saveData.settings.runWith,
            cache_key: normalizedKey,
            ai_fallback: saveData.settings.aiFallback ?? true,
            enable_self_healing: saveData.settings.enableSelfHealing ?? false,
            mask_secrets: saveData.settings.maskSecrets,
            code_version:
              saveData.settings.runWith === "code"
                ? (saveData.settings.codeVersion ?? 2)
                : undefined,
            workflow_definition: {
              version: saveData.workflowDefinitionVersion,
              parameters: saveData.parameters,
              blocks: saveData.blocks,
            },
            is_saved_task: saveData.workflow.is_saved_task,
            status: saveData.workflow.status,
            run_sequentially: saveData.settings.runSequentially,
            sequential_key: saveData.settings.sequentialKey,
          };

          workflowYaml = convertToYAML(requestBody);

          // Snapshot pre-submit canvas state (including unsaved local edits)
          // so Reject / Cancel / ERROR can revert the canvas to exactly what
          // the user submitted. ``saveData.workflow`` is the last-loaded
          // canonical; overlay it with the live canvas blocks/parameters.
          pendingSubmitSnapshot.current = {
            ...saveData.workflow,
            title: saveData.title,
            proxy_location: saveData.settings.proxyLocation,
            webhook_callback_url: saveData.settings.webhookCallbackUrl,
            persist_browser_session: saveData.settings.persistBrowserSession,
            reuse_browser_session: saveData.settings.reuseBrowserSession,
            pin_saved_session_ip: saveData.settings.pinSavedSessionIp,
            mask_secrets: saveData.settings.maskSecrets,
            browser_profile_id: saveData.settings.browserProfileId,
            browser_profile_key: saveData.settings.browserProfileKey,
            model: saveData.settings.model,
            workflow_definition: {
              ...saveData.workflow.workflow_definition,
              parameters: saveData.parameters,
              blocks: saveData.blocks,
            },
          } as WorkflowApiResponse;
        }

        if (messageAudioBlob) {
          try {
            const uploadResponse = await uploadDictationAudio(messageAudioBlob);
            chatIdForRequest = uploadResponse.workflow_copilot_chat_id;
            audioArtifactId = uploadResponse.audio_artifact_id;
          } catch (error) {
            console.warn("Failed to upload dictation audio:", error);
          }
        }

        const handleProcessingUpdate = (
          payload: WorkflowCopilotProcessingUpdate,
        ) => {
          const pendingId = pendingMessageId.current;
          if (!pendingId || !payload.timestamp) {
            return;
          }

          setMessages((prev) =>
            prev.map((message) =>
              message.id === pendingId
                ? { ...message, timestamp: payload.timestamp }
                : message,
            ),
          );
        };

        const handleResponse = (
          response: WorkflowCopilotStreamResponseUpdate,
          responseNarrative?: TurnNarrativeState,
        ) => {
          // Stream completed; a Cancel click after this point should no-op.
          pendingCancelToken.current = null;
          // Turn is terminal — the in-flight pause card (if any) unmounts with
          // the live bubble; drop its now-dead resume_token too.
          setLivePauseFrame(null);
          setWorkflowCopilotChatId(response.workflow_copilot_chat_id);
          setQuestionCancelToken(null);
          const receipts = response.narrative_payload?.questionInteractions as
            | QuestionInteraction[]
            | undefined;
          if (receipts)
            setQuestionInteractions((current) => [
              ...current.filter(
                (item) =>
                  !receipts.some(
                    (receipt) => receipt.interaction_id === item.interaction_id,
                  ),
              ),
              ...receipts,
            ]);

          // freeze the current narrative state into the AI message
          // so per-block cards persist as the user scrolls past this turn.
          // Read via narrativeRef because this callback was closed over at
          // handleSend time (pre-turn_start), so the React state binding is
          // stale here.
          const liveNarrative = responseNarrative ?? narrativeRef.current;
          const hasNarrativePayload =
            response.narrative_payload !== null &&
            typeof response.narrative_payload === "object";
          // A turn that streamed no narrative frames and carries no payload gets
          // no narrative at all: the persisted chat row has none either, so
          // fabricating one here would render this turn differently live than
          // after a reload — and route it past the gate that keys on a turn id.
          const frozenNarrative: TurnNarrativeState | undefined =
            liveNarrative.turnId !== null || hasNarrativePayload
              ? (responseNarrative ??
                applyNarrativeEvent(
                  liveNarrative.turnId !== null
                    ? liveNarrative
                    : EMPTY_NARRATIVE,
                  response,
                ))
              : undefined;

          const aiMessage: ChatMessage = {
            id: Date.now().toString(),
            sender: "ai",
            content: response.message,
            timestamp: response.response_time,
            narrative: frozenNarrative,
          };

          setMessages((prev) => [...prev, aiMessage]);
          const userCancelledThisTurn =
            cancelInFlightController.current === abortController;
          // A genuinely completed continuation keeps its optimistic "connected"
          // receipt; a cancelled one leaves the ref set for the finally to roll
          // back, so a canceled retry doesn't strand a false "Continuing…".
          if (!response.cancelled && !userCancelledThisTurn) {
            pendingTerminalContinuation.current = null;
          }
          // A failed run ends the turn with terminal "response", not "error";
          // an identical re-send after it is a legitimate retry, so it drains.
          const runFailed =
            frozenNarrative !== undefined &&
            notConfirmedOutcome(frozenNarrative) !== null;
          if (
            lastTurnRef.current &&
            !response.cancelled &&
            !userCancelledThisTurn &&
            frozenNarrative?.terminal !== "error" &&
            !runFailed
          ) {
            lastTurnRef.current.completedNormally = true;
          }
          const responseTurnId =
            response.turn_id ?? latestTurnId.current ?? null;
          const responseEntry = responseTurnId
            ? (turnSnapshots.current.get(responseTurnId) ?? null)
            : null;
          if (
            response.updated_workflow &&
            shouldAutoApplyWorkflowResponse(
              response,
              autoAccept,
              userCancelledThisTurn,
            )
          ) {
            applyWorkflowUpdate(response.updated_workflow, { applied: true });
            // This turn's auto-commit already moved canonical past any earlier
            // bypassed proposal — drop the stale handle so its gate cannot
            // reapply an outdated draft over what was just committed.
            setProposedWorkflow(null);
            setPendingProposalTurnId(null);
          } else if (response.updated_workflow) {
            setProposedWorkflow(response.updated_workflow);
            setPendingProposalTurnId(responseTurnId);
          } else if (
            // Cancel/error terminal on a turn that produced staged content →
            // snap canvas back to the pre-submit client snapshot.
            (response.cancelled || frozenNarrative?.terminal === "error") &&
            responseEntry?.hadStagedDraft &&
            responseEntry?.snapshot
          ) {
            applyWorkflowUpdate(responseEntry.snapshot);
            setProposedWorkflow(null);
            setPendingProposalTurnId(null);
          } else if (pendingProposalTurnId) {
            // No new draft this turn, but a bypassed proposal is still
            // pending: re-fetch instead of nulling, since the backend (given
            // keep_pending_proposal) may have kept it alive for a late Accept.
            void resyncProposalFromChatRow();
          } else {
            // Informational reply OR proposal pending review. For
            // proposals, the Accept/Reject card is the user's next gate;
            // canvas keeps the staged content until the user acts.
            setProposedWorkflow(response.updated_workflow ?? null);
            setPendingProposalTurnId(null);
          }
        };

        const handleError = (
          payload: WorkflowCopilotStreamErrorUpdate,
          errorNarrative?: TurnNarrativeState,
        ) => {
          pendingCancelToken.current = null;
          // A terminal error carries no credentialPause payload, so the dead
          // frame must be cleared explicitly or its card would stay actionable.
          setLivePauseFrame(null);
          const liveNarrative = errorNarrative ?? narrativeRef.current;
          const frozenNarrative: TurnNarrativeState | undefined =
            liveNarrative.turnId !== null
              ? (errorNarrative ?? applyNarrativeEvent(liveNarrative, payload))
              : undefined;
          const errorMessage: ChatMessage = {
            id: Date.now().toString(),
            sender: "ai",
            content: payload.error,
            narrative: frozenNarrative,
          };
          setMessages((prev) => [...prev, errorMessage]);
          // Error on a turn that produced staged content → snap canvas
          // back. Errors on no-draft turns leave the canvas alone.
          const errorTurnId = payload.turn_id ?? latestTurnId.current ?? null;
          const errorEntry = errorTurnId
            ? (turnSnapshots.current.get(errorTurnId) ?? null)
            : null;
          if (errorEntry?.hadStagedDraft && errorEntry?.snapshot) {
            applyWorkflowUpdate(errorEntry.snapshot);
            setProposedWorkflow(null);
            setPendingProposalTurnId(null);
          }
        };

        // Read before the awaits below, and unconditionally: this send owns the action, so a
        // throw on the way out must not leave it armed for whatever the user types next.
        const productAction = productActionRef.current;
        productActionRef.current = null;
        const requestCodeBlock = codeBlockModeEnabled
          ? codeBlockRequestOverride
          : false;
        const client = await getSseClient(credentialGetter);
        const targetBlockLabel = blockBuildTargetLabelRef.current;
        blockBuildTargetLabelRef.current = null;
        if (targetBlockLabel != null) {
          blockGenInFlightRef.current = true;
        }
        // Re-stamp from the values actually posted: the awaits above leave a
        // block Generate click room to arm the ref after the entry stamp.
        if (lastTurnRef.current) {
          lastTurnRef.current.hadBlockTarget = targetBlockLabel !== null;
          lastTurnRef.current.codeBlock = requestCodeBlock;
        }
        await client.postStreaming<WorkflowCopilotSsePayload>(
          "/workflow/copilot/chat-post",
          {
            workflow_id: workflowId,
            workflow_permanent_id: workflowPermanentId,
            workflow_copilot_chat_id: chatIdForRequest,
            workflow_run_id: productAction?.workflowRunId ?? workflowRunId,
            browser_session_id: liveBrowserSessionId ?? null,
            message: messageContent,
            selected_connected_account_id:
              options.selectedConnectedAccountId ?? null,
            audio_artifact_id: audioArtifactId,
            workflow_yaml: workflowYaml,
            mode: "build",
            code_block: requestCodeBlock,
            cancel_token: cancelToken,
            idempotency_key: options.idempotencyKey ?? null,
            target_block_label: targetBlockLabel,
            product_action: productAction?.action ?? null,
            selected_block_label: readSelectedBlockLabel(),
            keep_pending_proposal: Boolean(pendingProposalTurnId),
            supports_credential_pause: true,
            supports_question_tool: true,
          } as WorkflowCopilotChatRequest,
          (payload) => {
            // Same identity check the fallback timer makes: a frame buffered from an
            // aborted stream must not arm the turn that replaced it.
            if (streamingAbortController.current === abortController) {
              armStop();
            }
            switch (payload.type) {
              case "question_resolved":
                setQuestionInteractions((current) =>
                  current.map((item) =>
                    item.interaction_id === payload.interaction.interaction_id
                      ? payload.interaction
                      : item,
                  ),
                );
                setIsLoading(true);
                return false;
              case "question_required":
                setQuestionInteractions((current) => [
                  ...current.filter(
                    (item) =>
                      !payload.interactions.some(
                        (next) => next.interaction_id === item.interaction_id,
                      ),
                  ),
                  ...payload.interactions,
                ]);
                setQuestionCancelToken(payload.cancel_token);
                setWorkflowCopilotChatId(payload.workflow_copilot_chat_id);
                setIsLoading(false);
                return false;
              case "processing_update":
                handleProcessingUpdate(payload);
                return false;
              case "condensing":
                return false;
              case "tool_call":
              case "tool_result":
              case "narration":
                applyStoredNarrativeEvent(payload);
                return false;
              case "run_started":
                focusTurnRun(payload.workflow_run_id);
                return false;
              case "block_progress":
                followBuildLabel(payload.block_label);
                applyStoredNarrativeEvent(payload);
                // Earliest frame carrying the run id on a new backend — start
                // the live poll here so rows appear mid-execution.
                startRecordedActionsPoll(payload.workflow_run_id);
                focusTurnRun(payload.workflow_run_id);
                return false;
              case "run_outcome":
                applyStoredNarrativeEvent(payload);
                if (payload.workflow_run_id) {
                  rememberTurnOwnedRun(payload.workflow_run_id);
                  if (payload.verdict === "evaluating") {
                    // Fallback start against an old backend whose block_progress
                    // carried no run id: this is the first sighting.
                    startRecordedActionsPoll(payload.workflow_run_id);
                    focusTurnRun(payload.workflow_run_id);
                  } else {
                    // Terminal verdict: one convergent fetch, then stop polling.
                    void fetchRecordedActions(payload.workflow_run_id);
                    finalizeRecordedActionsPoll(payload.workflow_run_id);
                    releaseTurnRun(payload.workflow_run_id);
                  }
                }
                return false;
              case "title_update":
                // The title store is shared across workflow swaps in one Workspace, and a
                // stream survives the swap — so a frame from the workflow we left must not
                // name the one we are now looking at.
                if (payload.workflow_permanent_id !== workflowPermanentId) {
                  return false;
                }
                // Backend already persisted it; reload reads canonical. This only
                // moves the live title bar, and never over a user-chosen name.
                useWorkflowTitleStore
                  .getState()
                  .setTitleFromCopilotIfDefault(payload.title);
                return false;
              case "credential_required":
                setLivePauseFrame(payload);
                return false;
              case "turn_start": {
                // A new turn can't carry the prior turn's dead resume_token.
                setLivePauseFrame(null);
                // Move the pre-submit canvas snapshot into the per-turn
                // map keyed by the BE-assigned turn_id; cap the map so a
                // long-running chat does not retain every turn's snapshot.
                const map = turnSnapshots.current;
                map.set(payload.turn_id, {
                  snapshot: pendingSubmitSnapshot.current,
                  hadStagedDraft: false,
                });
                pendingSubmitSnapshot.current = null;
                while (map.size > MAX_TURN_SNAPSHOTS) {
                  const oldest = map.keys().next().value;
                  if (oldest === undefined) break;
                  map.delete(oldest);
                }
                latestTurnId.current = payload.turn_id;
                streamTurnId = payload.turn_id;
                // The chat this turn belongs to, read while the server is
                // announcing it. Reading the ref at arming time instead would
                // bind the poll to whatever chat the user switched to since.
                streamChatId = workflowCopilotChatIdRef.current;
                applyStoredNarrativeEvent(payload, EMPTY_NARRATIVE);
                return false;
              }
              case "design_start":
              case "design_end":
                applyStoredNarrativeEvent(payload);
                return false;
              case "workflow_draft": {
                // The draft frame summarizes the whole draft; the newest block
                // is the one being worked on.
                followBuildLabel(
                  payload.block_labels[payload.block_labels.length - 1] ?? null,
                );
                // Render the staged workflow on the canvas mid-turn. Only
                // mark the turn as having staged content if applyWorkflowUpdate
                // succeeds — a swallowed update would otherwise trigger a
                // spurious snap-back at terminal.
                if (payload.workflow) {
                  const applied = applyWorkflowUpdate(payload.workflow, {
                    midTurnDraft: true,
                  });
                  if (applied) {
                    const turnId = latestTurnId.current;
                    if (turnId) {
                      const entry = turnSnapshots.current.get(turnId);
                      if (entry) entry.hadStagedDraft = true;
                    }
                  }
                }
                applyStoredNarrativeEvent(payload);
                return false;
              }
              case "response": {
                const frozenNarrative = applyStoredNarrativeEvent(payload);
                sawTerminalFrame = true;
                handleResponse(payload, frozenNarrative);
                return true;
              }
              case "error": {
                const frozenNarrative = applyStoredNarrativeEvent(payload);
                sawTerminalFrame = true;
                handleError(payload, frozenNarrative);
                return true;
              }
              default:
                return false;
            }
          },
          { signal: abortController.signal },
        );
      } catch (error) {
        if (abortController.signal.aborted) {
          return;
        }
        console.error("Failed to send message:", error);
        if (options.idempotencyKey !== undefined && workflowCopilotChatId) {
          toast({
            title: "Checking account selection",
            description: RECOVERY_IN_PROGRESS_MESSAGE,
            variant: "destructive",
          });
          const generationBeforeRefresh = recoveryGeneration.current;
          await loadChatInPlace(workflowCopilotChatId);
          // loadChatInPlace stops recovery polls; forgive its own bump so this
          // severed stream still arms, but not a chat the user actually left.
          // It bumps exactly once, so a larger jump means a switch landed
          // during the await and this turn must not arm.
          if (
            generationBeforeRefresh === sendGeneration &&
            recoveryGeneration.current === generationBeforeRefresh + 1
          ) {
            sendGeneration = recoveryGeneration.current;
          }
        } else {
          const errorMessage: ChatMessage = {
            id: Date.now().toString(),
            sender: "ai",
            content: shouldArmRecovery()
              ? RECOVERY_IN_PROGRESS_MESSAGE
              : SEND_FAILED_MESSAGE,
          };
          setMessages((prev) => [...prev, errorMessage]);
        }
        // A thrown stream never emits a terminal narrative event, so clear the
        // bubble or its Working/elapsed indicator would tick forever.
        setNarrative(EMPTY_NARRATIVE);
        setLivePauseFrame(null);
      } finally {
        // Read before the clears below null cancelInFlightController. A soft Stop
        // sets it synchronously and only aborts 15s later, so the abort signal
        // alone would let a user cancel arm the recovery poll.
        const armRecovery = shouldArmRecovery();
        if (recoveryGeneration.current === sendGeneration) {
          if (streamingAbortController.current === abortController) {
            streamingAbortController.current = null;
            inFlightRef.current = false;
          }
          if (cancelInFlightController.current === abortController) {
            cancelInFlightController.current = null;
          }
          if (cancelSafetyTimer.current !== null) {
            clearTimeout(cancelSafetyTimer.current);
            cancelSafetyTimer.current = null;
          }
          if (stopArmTimer.current !== null) {
            clearTimeout(stopArmTimer.current);
            stopArmTimer.current = null;
          }
          setStopArmed(false);
          pendingMessageId.current = null;
          pendingCancelToken.current = null;
          // Backstop: roll back a continuation that didn't genuinely complete —
          // an error/thrown path already did, but a cancel or abort reaches only
          // here. A no-op once a genuine success cleared the ref above.
          rollbackPendingTerminalContinuation();
          setIsLoading(false);
          // Backstop: a turn that ends without a terminal run_outcome (thrown
          // stream) would otherwise leave a live poll running past the run.
          stopAllRecordedActionsPolls();
          releaseTurnRun();
          buildFollowEngaged.current = false;
          if (armRecovery && streamTurnId !== null) {
            startRecoveryPoll(
              streamChatId ?? workflowCopilotChatIdRef.current,
              streamTurnId,
            );
          }
        }
      }
    },
    [
      applyStoredNarrativeEvent,
      applyWorkflowUpdate,
      armStop,
      autoAccept,
      codeBlockModeEnabled,
      codeBlockRequestOverride,
      credentialGetter,
      fetchRecordedActions,
      finalizeRecordedActionsPoll,
      focusTurnRun,
      followBuildLabel,
      getSaveData,
      inputValue,
      questionInteractions,
      handleQuestionAnswer,
      isSpeechListening,
      isLiveBrowserReady,
      liveBrowserSessionId,
      loadChatInPlace,
      pendingProposalTurnId,
      releaseTurnRun,
      rememberTurnOwnedRun,
      startRecordedActionsPoll,
      stopAllRecordedActionsPolls,
      requiresLiveBrowser,
      resyncProposalFromChatRow,
      rollbackPendingTerminalContinuation,
      startRecoveryPoll,
      stopSpeech,
      takeSpeechAudioBlob,
      updateQueuedPrompt,
      uploadDictationAudio,
      workflowCopilotChatId,
      workflowPermanentId,
      workflowRunId,
    ],
  );
  useEffect(() => {
    handleSendRef.current = handleSend;
  }, [handleSend]);

  const handleTestEndToEnd = useCallback(() => {
    productActionRef.current = { action: "test_end_to_end" };
    void handleSend(TEST_END_TO_END_PROMPT);
  }, [handleSend]);

  const handleConnectedAccountChoice = useCallback(
    (turnId: string, connectionId: string) => {
      if (hasPendingQuestion || connectedAccountChoiceLatch.current !== null) {
        return;
      }
      // Build this before taking the latch so an unavailable UUID API cannot
      // leave the picker permanently disabled in a dev or embedded context.
      const idempotencyKey = `connected-account:${turnId}:${connectionId}:${crypto.randomUUID()}`;
      connectedAccountChoiceLatch.current = turnId;
      setConnectedAccountChoicePendingTurnId(turnId);
      void handleSend(connectionId, {
        selectedConnectedAccountId: connectionId,
        // A fresh explicit click is a fresh attempt. Transport retries retain
        // this request body, while a recovered interrupted turn gets a new key.
        idempotencyKey,
      }).finally(() => {
        if (connectedAccountChoiceLatch.current === turnId) {
          connectedAccountChoiceLatch.current = null;
          setConnectedAccountChoicePendingTurnId(null);
        }
      });
    },
    [handleSend, hasPendingQuestion],
  );

  // A code block's "Generate" button asks the copilot to (re)build that one block
  // from its goal. Force build + code mode, then fire the send on the next tick.
  const pendingBlockBuild = useCopilotActionStore(
    (state) => state.pendingBuild,
  );
  const clearPendingBlockBuild = useCopilotActionStore(
    (state) => state.clearPendingBuild,
  );
  const finishBlockGenerating = useCopilotActionStore(
    (state) => state.finishGenerating,
  );
  const blockCancelNonce = useCopilotActionStore((state) => state.cancelNonce);
  const blockBuildMessageRef = useRef<string | null>(null);
  const [blockBuildArmNonce, setBlockBuildArmNonce] = useState(0);

  useEffect(() => {
    if (!pendingBlockBuild) {
      return;
    }
    blockBuildMessageRef.current =
      `Rebuild the "${pendingBlockBuild.blockLabel}" code block so it accomplishes ` +
      `this goal, and update its code and steps accordingly: ${pendingBlockBuild.prompt}`;
    blockBuildTargetLabelRef.current = pendingBlockBuild.blockLabel;
    setCodeWorkflow(true);
    setCodeBlockRequestOverride(true);
    setBlockBuildArmNonce((nonce) => nonce + 1);
    clearPendingBlockBuild();
  }, [pendingBlockBuild, clearPendingBlockBuild]);

  useEffect(() => {
    if (blockBuildArmNonce === 0 || blockBuildMessageRef.current === null) {
      return;
    }
    if (!codeWorkflow) {
      return;
    }
    const message = blockBuildMessageRef.current;
    blockBuildMessageRef.current = null;
    // A prompt is already queued, so this send no-ops. Disarm the block target
    // (else the queued drain inherits it) and clear the stuck generating state.
    if (queuedPromptRef.current) {
      blockBuildTargetLabelRef.current = null;
      finishBlockGenerating();
      return;
    }
    void handleSend(message);
  }, [blockBuildArmNonce, codeWorkflow, handleSend, finishBlockGenerating]);

  const blockGenLoadingRef = useRef(isLoading);
  useEffect(() => {
    if (
      blockGenLoadingRef.current &&
      !isLoading &&
      blockGenInFlightRef.current
    ) {
      blockGenInFlightRef.current = false;
      finishBlockGenerating();
    }
    blockGenLoadingRef.current = isLoading;
  }, [isLoading, finishBlockGenerating]);

  const blockCancelNonceRef = useRef(blockCancelNonce);
  useEffect(() => {
    if (blockCancelNonce !== blockCancelNonceRef.current) {
      blockCancelNonceRef.current = blockCancelNonce;
      const queued = queuedPromptRef.current;
      // A queued block build hasn't streamed yet, so cancelSend would no-op and
      // let it drain later. Drop it (and its bubble), leaving any unrelated
      // in-flight turn untouched.
      if (queued && blockBuildTargetLabelRef.current != null) {
        blockBuildTargetLabelRef.current = null;
        updateQueuedPrompt(null);
        setMessages((prev) =>
          prev.filter((message) => message.id !== queued.id),
        );
        return;
      }
      void cancelSend("stop_button", { requireArmed: false });
    }
  }, [blockCancelNonce, cancelSend, updateQueuedPrompt]);

  const handleKeyPress = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Escape and Enter both mean something to an IME mid-composition: dismissing a
    // conversion candidate and committing one. React's synthetic event does not carry
    // isComposing, so the native one is what can be asked.
    if (e.nativeEvent.isComposing) {
      return;
    }
    if (e.key === "Escape" && queuedPrompt) {
      e.preventDefault();
      e.stopPropagation();
      restoreQueuedPromptToComposer();
      return;
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  useEffect(() => {
    if (!queuedPrompt || hasPendingQuestion) {
      return;
    }
    // isLoading (reactive state) is the in-flight signal here so the effect
    // re-runs when a turn ends; handleSend uses the synchronous ref instead.
    const lastTurn = lastTurnRef.current;
    const drainAction = resolveDrainAction({
      queuedReason: queuedPrompt.reason,
      inFlight: isLoading,
      hasLiveBrowserSession: Boolean(liveBrowserSessionId),
      hasWorkflowPermanentId: Boolean(workflowPermanentId),
      queuedContent: queuedPrompt.content,
      turnOpeningContent: lastTurn?.content ?? null,
      turnCompletedNormally: lastTurn?.completedNormally ?? false,
      turnWorkflowMatches:
        lastTurn?.workflowPermanentId === workflowPermanentId,
      turnRequestMatches:
        lastTurn?.hadAudio === false &&
        lastTurn?.hadBlockTarget === false &&
        lastTurn?.browserSessionId === (liveBrowserSessionId ?? null) &&
        lastTurn?.codeBlock ===
          (codeBlockModeEnabled ? codeBlockRequestOverride : false) &&
        (queuedPrompt.audioBlob ?? null) === null &&
        queuedPrompt.selectedConnectedAccountId === undefined &&
        blockBuildTargetLabelRef.current === null,
    });
    if (drainAction === "wait") {
      return;
    }

    if (drainAction === "drop_duplicate") {
      const dropped = queuedPrompt;
      updateQueuedPrompt(null);
      setMessages((prev) =>
        prev.filter((message) => message.id !== dropped.id),
      );
      return;
    }

    const promptToSend = queuedPrompt;
    // Clear before re-entering handleSend so a 'send' resolution leaves no
    // stale queued prompt for the effect to re-drain in a loop. A working
    // prompt that still needs the browser re-queues under the same id and
    // drains via the live_browser path once the session arrives.
    updateQueuedPrompt(null);
    handleSend(promptToSend.content, {
      queuedMessageId: promptToSend.id,
      skipQueue: drainAction === "drain_skip_queue",
      audioBlob: promptToSend.audioBlob,
      idempotencyKey: promptToSend.idempotencyKey,
      selectedConnectedAccountId: promptToSend.selectedConnectedAccountId,
    }).catch((error) => {
      console.error("Queued send failed:", error);
    });
  }, [
    codeBlockModeEnabled,
    codeBlockRequestOverride,
    handleSend,
    hasPendingQuestion,
    isLoading,
    liveBrowserSessionId,
    queuedPrompt,
    updateQueuedPrompt,
    workflowPermanentId,
  ]);

  useEffect(() => {
    if (!autoSendMessage || hasAutoSentRef.current) {
      return;
    }
    if (isLoadingHistory || isLoading || !workflowPermanentId || queuedPrompt) {
      return;
    }
    // Synchronous gate: isLoadingHistory state is stale in this effect's
    // closure when both effects run in the same commit.
    if (historyLoadedForRef.current !== workflowPermanentId) {
      return;
    }
    const saveData = getSaveData();
    if (
      !saveData?.workflow.workflow_id ||
      saveData.workflow.workflow_permanent_id !== workflowPermanentId
    ) {
      return;
    }
    // Trip the guard before any await so the 5s timeout cannot toast over
    // an in-flight send. handleSend internally routes to the queue when the
    // live browser isn't ready yet.
    hasAutoSentRef.current = true;
    onInitialMessageConsumedRef.current?.();
    if (initialAction) {
      productActionRef.current = {
        action: initialAction.kind,
        workflowRunId: initialAction.workflowRunId,
      };
    }
    handleSend(autoSendMessage).catch((error) => {
      console.error("Auto-send failed:", error);
    });
  }, [
    handleSend,
    autoSendMessage,
    initialAction,
    isLoading,
    isLoadingHistory,
    queuedPrompt,
    getSaveData,
    workflowPermanentId,
  ]);

  useEffect(() => {
    if (!autoSendMessage || hasAutoSentRef.current) {
      return;
    }
    if (
      isLoadingHistory ||
      isLoading ||
      isWaitingForLiveBrowser ||
      queuedPrompt
    ) {
      return;
    }
    const saveData = getSaveData();
    if (!saveData?.workflow.workflow_id) {
      return;
    }
    const timer = window.setTimeout(() => {
      if (hasAutoSentRef.current) return;
      hasAutoSentRef.current = true;
      onInitialMessageConsumedRef.current?.();
      toast({
        title: "Could not auto-send message",
        description: initialAction
          ? "The copilot was not ready in time — click Fix with Copilot again."
          : "The copilot was not ready in time — please retype your prompt.",
        variant: "destructive",
      });
    }, AUTO_SEND_TIMEOUT_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, [
    autoSendMessage,
    initialAction,
    isLoadingHistory,
    isLoading,
    isWaitingForLiveBrowser,
    queuedPrompt,
    getSaveData,
  ]);

  const handleMouseDown = (e: React.MouseEvent) => {
    setIsDragging(true);
    setDragStart({
      x: e.clientX - position.x,
      y: e.clientY - position.y,
    });
  };

  const handleResizeMouseDown = (
    e: React.MouseEvent,
    direction: "n" | "s" | "e" | "w" | "se" | "sw" | "ne" | "nw",
  ) => {
    e.preventDefault();
    e.stopPropagation();
    setIsResizing(true);
    setResizeDirection(direction);
    setResizeStart({
      x: e.clientX,
      y: e.clientY,
      width: size.width,
      height: size.height,
      posX: position.x,
      posY: position.y,
    });
  };

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (isDragging) {
        setPosition({
          x: e.clientX - dragStart.x,
          y: e.clientY - dragStart.y,
        });
      }
      if (isResizing) {
        const deltaX = e.clientX - resizeStart.x;
        const deltaY = e.clientY - resizeStart.y;

        let newWidth = resizeStart.width;
        let newHeight = resizeStart.height;
        let newX = resizeStart.posX;
        let newY = resizeStart.posY;

        // Corners
        if (resizeDirection === "se") {
          // Southeast: resize from bottom-right
          newWidth = Math.max(MIN_WINDOW_WIDTH, resizeStart.width + deltaX);
          newHeight = Math.max(MIN_WINDOW_HEIGHT, resizeStart.height + deltaY);
        } else if (resizeDirection === "sw") {
          // Southwest: resize from bottom-left
          newWidth = Math.max(MIN_WINDOW_WIDTH, resizeStart.width - deltaX);
          newHeight = Math.max(MIN_WINDOW_HEIGHT, resizeStart.height + deltaY);
          if (resizeStart.width - deltaX >= MIN_WINDOW_WIDTH) {
            newX = resizeStart.posX + deltaX;
          }
        } else if (resizeDirection === "ne") {
          // Northeast: resize from top-right
          newWidth = Math.max(MIN_WINDOW_WIDTH, resizeStart.width + deltaX);
          newHeight = Math.max(MIN_WINDOW_HEIGHT, resizeStart.height - deltaY);
          if (resizeStart.height - deltaY >= MIN_WINDOW_HEIGHT) {
            newY = resizeStart.posY + deltaY;
          }
        } else if (resizeDirection === "nw") {
          // Northwest: resize from top-left
          newWidth = Math.max(MIN_WINDOW_WIDTH, resizeStart.width - deltaX);
          newHeight = Math.max(MIN_WINDOW_HEIGHT, resizeStart.height - deltaY);
          if (resizeStart.width - deltaX >= MIN_WINDOW_WIDTH) {
            newX = resizeStart.posX + deltaX;
          }
          if (resizeStart.height - deltaY >= MIN_WINDOW_HEIGHT) {
            newY = resizeStart.posY + deltaY;
          }
        }
        // Edges
        else if (resizeDirection === "n") {
          // North: resize from top
          newHeight = Math.max(MIN_WINDOW_HEIGHT, resizeStart.height - deltaY);
          if (resizeStart.height - deltaY >= MIN_WINDOW_HEIGHT) {
            newY = resizeStart.posY + deltaY;
          }
        } else if (resizeDirection === "s") {
          // South: resize from bottom
          newHeight = Math.max(MIN_WINDOW_HEIGHT, resizeStart.height + deltaY);
        } else if (resizeDirection === "e") {
          // East: resize from right
          newWidth = Math.max(MIN_WINDOW_WIDTH, resizeStart.width + deltaX);
        } else if (resizeDirection === "w") {
          // West: resize from left
          newWidth = Math.max(MIN_WINDOW_WIDTH, resizeStart.width - deltaX);
          if (resizeStart.width - deltaX >= MIN_WINDOW_WIDTH) {
            newX = resizeStart.posX + deltaX;
          }
        }

        setSize({
          width: newWidth,
          height: newHeight,
        });
        setPosition({
          x: newX,
          y: newY,
        });
      }
    };

    const handleMouseUp = () => {
      setIsDragging(false);
      setIsResizing(false);
    };

    if (isDragging || isResizing) {
      document.addEventListener("mousemove", handleMouseMove);
      document.addEventListener("mouseup", handleMouseUp);
    }

    return () => {
      document.removeEventListener("mousemove", handleMouseMove);
      document.removeEventListener("mouseup", handleMouseUp);
    };
  }, [isDragging, dragStart, isResizing, resizeStart, resizeDirection]);

  // Handle window resize to keep chat window within viewport
  useEffect(() => {
    const handleResize = () => {
      setPosition((prev) =>
        constrainPosition(prev.x, prev.y, size.width, size.height),
      );
    };

    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [size]);

  // Recalculate position when chat opens to align with button (only first time)
  useLayoutEffect(() => {
    if (isOpen && buttonRef?.current && !hasInitializedPosition.current) {
      const newPosition = calculateDefaultPosition(
        size.width,
        size.height,
        buttonRef,
      );
      setPosition(newPosition);
      hasInitializedPosition.current = true;
    }
  }, [isOpen, buttonRef, size.width, size.height]);

  const autoBoundReceiptIndexes = useMemo(
    () =>
      selectAutoBoundReceiptIndexes(
        messages.map(
          (message) => autoBoundReceiptFor(message)?.credentialId ?? null,
        ),
      ),
    [messages],
  );

  // Stoppable between the turn's first streamed frame and its terminal one; isLoading
  // alone is true from send. Issuing a cancel clears the token, so isStopping holds it.
  const turnObservablyRunning =
    isStopping ||
    (isLoading &&
      stopArmed &&
      narrative.terminal === null &&
      pendingCancelToken.current !== null);
  // A render-phase write would let a discarded pass latch, and a passive effect
  // would leave Stop painted armed while cancelSend still reads false.
  useLayoutEffect(() => {
    turnObservablyRunningRef.current = turnObservablyRunning;
  }, [turnObservablyRunning]);

  if (!isOpen) {
    return null;
  }

  const queuedPromptWaitingStatus =
    queuedPrompt?.reason === "working"
      ? "Queued — sends when this turn finishes."
      : "Prompt queued. Waiting for live browser...";
  // queuedPromptWaitingStatus already surfaces via the composer chip
  // (working reason) or the queued bubble's footer (live_browser reason);
  // don't duplicate it as a second status line above the composer.
  const browserStatusText = isWaitingForLiveBrowser
    ? "Live browser is starting. Your next send will wait until it connects."
    : null;
  const inputStatusText = isSpeechListening
    ? browserStatusText
      ? `Listening… · ${browserStatusText}`
      : "Listening…"
    : browserStatusText;
  const lastTurnIndex = findLastTurnIndex(messages);
  // The composer is the answer path for anything the card cannot take, so while a question
  // record is still pending it says so rather than inviting a new request.
  const latestTurnIsAsk = questionInteractions.some(
    (item) => item.status === "pending",
  );
  // A bypassed proposal's gate stays attached to its owning turn (not
  // necessarily the last message) so a chip can jump back to it.
  const gateOwnerIndex = pendingProposalTurnId
    ? findLastIndexOfTurn(messages, pendingProposalTurnId)
    : -1;
  const gateIndex = gateOwnerIndex >= 0 ? gateOwnerIndex : lastTurnIndex;
  const gateOwnerNarrative = messages[gateIndex]?.narrative;
  // Mid-turn Accept would be clobbered by the in-flight turn's terminal
  // restore, so gate actions wait for idle.
  const gateActionable =
    Boolean(proposedWorkflow) && !isLoading && !isLoadingHistory;
  const hasComposerText = inputValue.trim().length > 0;
  // The cycling verb row plus the stop button's orbiting ring carry the
  // working state, so the prose status line and the queued chip stand down.
  const showWorkingRow = isLoading;
  // A live_browser-reason queued prompt parks with no active turn to stop, so
  // an empty composer's morph button would render as a guaranteed no-op "Send".
  // With text typed it does act — it rewrites the parked prompt.
  const waitingOnQueueOnly =
    queuedPrompt?.reason === "live_browser" && !hasComposerText;
  // When the initial history load drops the queued bubble, the composer chip
  // takes over its live_browser status/Cancel (footer-else-chip).
  const queuedBubbleOrphaned = Boolean(
    queuedPrompt &&
    queuedPrompt.reason === "live_browser" &&
    !messages.some((message) => message.id === queuedPrompt.id),
  );
  const showsStopGlyph =
    isStopping || (turnObservablyRunning && !hasComposerText);
  // Sent, no frame yet: the control reports the wait rather than an action, and
  // cancelSend's own guard is what makes a press in this window issue no cancel.
  const turnPendingFirstFrame =
    isLoading && !turnObservablyRunning && narrative.terminal === null;
  const morphButtonPending = turnPendingFirstFrame && !hasComposerText;
  const morphButtonLabel = isStopping
    ? "Stopping…"
    : waitingOnQueueOnly
      ? "Send disabled — waiting for live browser"
      : morphButtonPending
        ? "Starting…"
        : queuedPrompt && hasComposerText
          ? "Replace queued message"
          : !turnObservablyRunning
            ? isLoading
              ? "Queue for next turn"
              : "Send"
            : hasComposerText
              ? "Queue for next turn"
              : "Stop";
  // Shared between the composer treatments so the two Build implementations
  // never drift.
  const modeMenuItems = (
    <>
      <DropdownMenuItem
        aria-label="Build"
        onSelect={() => {
          setCodeWorkflow(false);
          setCodeBlockRequestOverride(false);
        }}
        className={cn("flex items-start gap-2.5", !codeWorkflow && "bg-accent")}
      >
        <ModeGlyph />
        <span className="flex flex-1 flex-col">
          <span className="text-sm font-medium">Build</span>
          <span className="text-xs leading-snug text-muted-foreground">
            Navigates the site to design your workflow, then tests that it
            works.
          </span>
        </span>
        {!codeWorkflow ? (
          <CheckIcon className="h-4 w-4 text-sky-700 dark:text-sky-400" />
        ) : null}
      </DropdownMenuItem>
      {codeOptionAvailable ? (
        <DropdownMenuItem
          aria-label="Build with code"
          onSelect={() => {
            setCodeWorkflow(true);
            setCodeBlockRequestOverride(true);
          }}
          className={cn(
            "flex items-start gap-2.5",
            codeWorkflow && "bg-accent",
          )}
        >
          <ModeGlyph glow />
          <span className="flex flex-1 flex-col">
            <span className="text-sm font-medium">Build with code</span>
            <span className="text-xs leading-snug text-muted-foreground">
              Build the workflow as code. Faster and more flexible, but may need
              extra detail to handle every edge case.
            </span>
          </span>
          {codeWorkflow ? (
            <CheckIcon className="h-4 w-4 text-sky-700 dark:text-sky-400" />
          ) : null}
        </DropdownMenuItem>
      ) : null}
    </>
  );

  const renderQuestionCard = (interaction: QuestionInteraction) => (
    <QuestionPartsCard
      key={interaction.interaction_id}
      interaction={interaction}
      disabled={isSubmittingQuestion}
      onAnswer={(response) => void handleQuestionAnswer(interaction, response)}
    />
  );

  const content = (
    <div
      className={
        docked
          ? chromeless
            ? "relative flex h-full w-full flex-col overflow-hidden text-foreground"
            : "relative flex h-full w-full flex-col overflow-hidden rounded-lg border border-border bg-slate-elevation1 text-foreground"
          : "fixed z-50 flex flex-col rounded-lg border border-border bg-slate-elevation1 text-foreground shadow-2xl"
      }
      style={
        docked
          ? undefined
          : {
              left: `${position.x}px`,
              top: `${position.y}px`,
              width: `${size.width}px`,
              height: `${size.height}px`,
            }
      }
    >
      {/* Header. The studio (chromeless) hosts History/New chat in its
          Copilot pane header via useCopilotHeaderStore — no row here. */}
      {chromeless ? null : (
        <div
          className={
            "flex items-center border-b border-border px-4" +
            (docked
              ? " h-14 shrink-0 justify-between"
              : " cursor-move justify-between py-2")
          }
          onMouseDown={docked ? undefined : handleMouseDown}
        >
          {chromeless ? null : (
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold text-foreground">
                {docked ? "Copilot" : "Agent Copilot (Beta)"}
              </h3>
              {docked ? (
                <span className="rounded bg-slate-elevation3 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                  Beta
                </span>
              ) : null}
            </div>
          )}
          <div className="flex items-center gap-2">
            <WorkflowCopilotHistory
              workflowPermanentId={workflowPermanentId}
              currentChatId={workflowCopilotChatId}
              onSelect={handleSelectHistoryChat}
              disabled={isLoading || isLoadingHistory}
            />
            <button
              type="button"
              onClick={handleNewChat}
              onMouseDown={(e) => e.stopPropagation()}
              className="rounded border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-accent-foreground"
            >
              New chat
            </button>
            <div className="h-2 w-2 rounded-full bg-emerald-500"></div>
            <span className="text-xs text-muted-foreground">Active</span>
            {/* Only the floating window closes itself; docked chrome is external. */}
            {docked ? null : (
              <button
                type="button"
                onClick={() => onClose?.()}
                onMouseDown={(e) => e.stopPropagation()}
                className="ml-2 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                title="Close"
              >
                <Cross2Icon className="h-4 w-4" />
              </button>
            )}
          </div>
        </div>
      )}

      {/* Messages */}
      <div className="relative min-h-0 flex-1">
        <div ref={scrollRef} className="h-full overflow-y-auto p-4">
          <div className="space-y-3">
            {!isLoadingHistory && messages.length === 0 && !isLoading ? (
              <div className="rounded-lg border border-border bg-slate-elevation2 p-4 text-sm text-muted-foreground">
                <p className="font-semibold text-foreground">
                  Start a new chat
                </p>
                <p className="mt-2 text-muted-foreground">
                  Ask the copilot to draft or edit your agent. Provide a goal,
                  the target site, and any credentials it should use.
                </p>
                <p className="mt-2 text-muted-foreground">
                  Example: "Build an agent to find the top post on hackernews
                  today"
                </p>
                {/* The only in-product pointer to this: the newer composer
                    placeholder dropped the "or paste recorded steps" clause, so
                    without it here the affordance is undiscoverable. */}
                <p className="mt-2 text-muted-foreground">
                  Already recorded this with another agent? Copy that workflow's
                  prompt text and paste it here.
                </p>
              </div>
            ) : null}
            <ConvoAggregatePill
              messages={messages}
              hasPendingQuestion={questionInteractions.some(
                (item) => item.status === "pending",
              )}
              isInFlight={
                !hasPendingQuestion &&
                (isLoading ||
                  (narrative.turnId !== null && narrative.terminal === null))
              }
            />
            {messages.flatMap((message, index) => {
              const rendered = (() => {
                const isLastMessage = index === lastTurnIndex;
                if (
                  message.kind === "run_lifecycle" ||
                  (message.sender === "product" &&
                    message.id !== queuedPrompt?.id)
                ) {
                  return (
                    <RunLifecycleLine
                      key={message.id}
                      content={message.content}
                    />
                  );
                }
                if (message.kind === "status_notice") {
                  return (
                    <div key={message.id} role="status" aria-live="polite">
                      <MessageItem message={message} />
                    </div>
                  );
                }
                // Per-message frozen narrative. When an AI message carries a
                // frozen narrative, render the narrative card stack in place
                // of the legacy text bubble so the per-block cards survive
                // subsequent turns. The Accept/Reject controls render only on
                // the latest message AND while the proposal is pending review.
                if (message.sender === "ai" && message.narrative) {
                  const turnId = message.narrative.turnId;
                  const choices = message.narrative.connectedAccountChoices;
                  const adjacentMessage = nextAnsweringMessage(messages, index);
                  const selectedConnectionId =
                    adjacentMessage?.sender === "user" &&
                    choices.some(
                      (choice) =>
                        choice.connection_id === adjacentMessage.content,
                    )
                      ? adjacentMessage.content
                      : null;
                  // Once any later conversation message exists, this account-choice turn is
                  // historical. Only an exact structured selection may render a receipt;
                  // prose must never make the old card actionable again between stream
                  // completion and the next assistant response.
                  const hasUnconsumedAdjacentMessage =
                    adjacentMessage !== undefined &&
                    selectedConnectionId === null;
                  const showReviewGate =
                    shouldShowDiffCard(message.narrative) ||
                    (turnId !== null && turnId === pendingProposalTurnId);
                  return (
                    <div
                      key={message.id}
                      className="flex flex-col gap-2"
                      role="status"
                      aria-live="polite"
                    >
                      <NarrativeView
                        turn={message.narrative}
                        onBlockSelect={onBlockSelect}
                        workingRowActive={showWorkingRow}
                      />
                      {isLastMessage &&
                      hasPendingQuestion &&
                      (choices.length > 0 ||
                        message.narrative.googleConnectionNotices.some(
                          (notice) => notice.condition === "unbound",
                        )) ? (
                        <p className="text-xs text-muted-foreground">
                          Answer or cancel the pending question before choosing
                          a Google account.
                        </p>
                      ) : null}
                      {turnId !== null && choices.length > 0 ? (
                        <ConnectedAccountChoiceCard
                          choices={choices}
                          selectedConnectionId={selectedConnectionId}
                          disabled={
                            !isLastMessage ||
                            isLoading ||
                            hasPendingQuestion ||
                            hasUnconsumedAdjacentMessage ||
                            selectedConnectionId !== null ||
                            connectedAccountChoicePendingTurnId === turnId
                          }
                          onSelect={(connectionId) =>
                            handleConnectedAccountChoice(turnId, connectionId)
                          }
                        />
                      ) : null}
                      {message.narrative.googleConnectionNotices.map(
                        (notice) => (
                          <GoogleReconnectCard
                            key={notice.connectionId ?? "unbound"}
                            notice={notice}
                            selectedConnectionId={
                              adjacentMessage?.sender === "user"
                                ? adjacentMessage.content
                                : null
                            }
                            disabled={
                              !isLastMessage ||
                              isLoading ||
                              hasPendingQuestion ||
                              turnId === null ||
                              connectedAccountChoicePendingTurnId === turnId
                            }
                            onSelect={(connectionId) => {
                              if (turnId !== null)
                                handleConnectedAccountChoice(
                                  turnId,
                                  connectionId,
                                );
                            }}
                          />
                        ),
                      )}
                      {showReviewGate ? (
                        <ReviewGateCard
                          turn={message.narrative}
                          pending={
                            index === gateIndex && Boolean(proposedWorkflow)
                          }
                          verdict={getReviewGateVerdict(
                            message.narrative,
                            proposedWorkflow,
                          )}
                          settled={
                            turnId && acceptedTurnIds.has(turnId)
                              ? "accepted"
                              : turnId && rejectedTurnIds.has(turnId)
                                ? "rejected"
                                : null
                          }
                          actionsEnabled={gateActionable}
                          onAccept={() =>
                            proposedWorkflow &&
                            handleAcceptWorkflow(proposedWorkflow)
                          }
                          onAlwaysAccept={() =>
                            proposedWorkflow &&
                            handleAcceptWorkflow(proposedWorkflow, true)
                          }
                          onReject={handleRejectWorkflow}
                          onReview={() =>
                            proposedWorkflow &&
                            handleReviewWorkflow(proposedWorkflow)
                          }
                          onTestEndToEnd={handleTestEndToEnd}
                          gateId={turnId ? `copilot-gate-${turnId}` : undefined}
                          flash={turnId !== null && turnId === gateFlashTurnId}
                        />
                      ) : null}
                      {!isLoadingHistory &&
                      isLastMessage &&
                      shouldShowConfirmCard(message.narrative) ? (
                        <ConfirmCard
                          onConfirm={() => handleSend("Confirmed.")}
                          onChangeInstead={() => {
                            textareaRef.current?.focus();
                            adjustTextareaHeight();
                          }}
                        />
                      ) : null}
                      {(() => {
                        if (isLoadingHistory || turnId === null) return null;
                        const credFrame = credentialCardFrameFor(
                          message.narrative,
                        );
                        if (!credFrame) return null;
                        const localResolution = credentialResolutions[turnId];
                        const resolvedOutcome =
                          localResolution ??
                          historicalCredentialOutcome(message.narrative);
                        // The actionable ask is only live on the tail message; a resolved receipt still
                        // renders on any message so a scrolled-back turn keeps its outcome. Without this,
                        // picking on a stale card would show a receipt with no backend call or continue.
                        // A stranded ask (its auto-continue failed) stays actionable off-tail for a retry.
                        if (
                          !resolvedOutcome &&
                          !isLastMessage &&
                          !strandedTerminalContinuations.has(turnId)
                        )
                          return null;
                        return (
                          <CredentialCard
                            frame={credFrame}
                            mode="terminal"
                            reloadKey={credentialsReloadKey}
                            resolvedOutcome={resolvedOutcome}
                            continued={Boolean(localResolution?.continued)}
                            // A picked credential (id + name from the fetched list) auto-continues by
                            // id; the Add-credential CTA (no id) opens the modal instead. A stranded ask
                            // (its prior continue failed) may continue too, though it is no longer the tail.
                            onConnect={(credentialId, name) => {
                              const canContinue =
                                isLastMessage ||
                                strandedTerminalContinuations.has(turnId);
                              return credentialId
                                ? continueAfterTerminalConnect(
                                    turnId,
                                    credentialId,
                                    name ?? localResolution?.name,
                                    canContinue,
                                  )
                                : openCredentialModal(
                                    null,
                                    turnId,
                                    canContinue,
                                  );
                            }}
                            onSkip={() =>
                              resolveTerminalCredential(turnId, "skip")
                            }
                          />
                        );
                      })()}
                      {(() => {
                        if (isLoadingHistory || turnId === null) return null;
                        const autoBound = autoBoundReceiptFor(message);
                        if (!autoBound || !autoBoundReceiptIndexes.has(index))
                          return null;
                        // The receipt renders on any message (scrollback-safe). Before a Change it shows
                        // the auto-bound credential with a Change picker; after one, the local resolution
                        // routes into CredentialCard's existing "Continuing with 'X'…" receipt.
                        const localResolution = credentialResolutions[turnId];
                        const canContinue =
                          isLastMessage ||
                          strandedTerminalContinuations.has(turnId);
                        return (
                          <CredentialCard
                            frame={{
                              type: "credential_required",
                              reason: "workflow_credential_inputs_unbound",
                            }}
                            mode="auto-bound"
                            autoBound={autoBound}
                            // Not while a turn is in flight: the shared continue path would still record
                            // an optimistic "connected" even though it suppresses the actual send.
                            canChange={canContinue && !isLoading}
                            reloadKey={credentialsReloadKey}
                            resolvedOutcome={localResolution}
                            continued={Boolean(localResolution?.continued)}
                            // Change re-enters the same terminal-continue path a terminal ask pick uses
                            // (send "Use the credential <id> — continue", or open the add modal). A silent
                            // bind never has a live pause, so never the typed credential-response path.
                            onConnect={(credentialId, name) =>
                              credentialId
                                ? continueAfterTerminalConnect(
                                    turnId,
                                    credentialId,
                                    name ?? localResolution?.name,
                                    canContinue,
                                  )
                                : openCredentialModal(null, turnId, canContinue)
                            }
                            onSkip={() => {}}
                          />
                        );
                      })()}
                    </div>
                  );
                }
                const isGateOwnerOrLast =
                  index === gateIndex && Boolean(proposedWorkflow);
                const selectionReceipt = connectedAccountSelectionReceipt(
                  messages,
                  index,
                );
                return (
                  <MessageItem
                    key={message.id}
                    message={
                      selectionReceipt
                        ? { ...message, content: selectionReceipt }
                        : message
                    }
                    queuedStatus={
                      queuedPrompt?.reason === "live_browser" &&
                      queuedPrompt.id === message.id
                        ? {
                            text: queuedPromptWaitingStatus,
                            onCancel: restoreQueuedPromptToComposer,
                          }
                        : null
                    }
                    footer={
                      isGateOwnerOrLast ? (
                        <ReviewGateCard
                          pending
                          verdict={getReviewGateVerdict(
                            gateOwnerNarrative,
                            proposedWorkflow,
                          )}
                          settled={null}
                          actionsEnabled={gateActionable}
                          onAccept={() =>
                            proposedWorkflow &&
                            handleAcceptWorkflow(proposedWorkflow)
                          }
                          onAlwaysAccept={() =>
                            proposedWorkflow &&
                            handleAcceptWorkflow(proposedWorkflow, true)
                          }
                          onReject={handleRejectWorkflow}
                          onReview={() =>
                            proposedWorkflow &&
                            handleReviewWorkflow(proposedWorkflow)
                          }
                          onTestEndToEnd={handleTestEndToEnd}
                        />
                      ) : null
                    }
                  />
                );
              })();
              const questions = questionInteractions
                .filter((item) => item.turn_id === message.narrative?.turnId)
                .map(renderQuestionCard);
              return [...questions, rendered];
            })}
            {questionInteractions
              .filter(
                (item) =>
                  !messages.some(
                    (message) => message.narrative?.turnId === item.turn_id,
                  ),
              )
              .map(renderQuestionCard)}
            {questionCancelToken &&
            questionInteractions.some((item) => item.status === "pending") ? (
              <Button
                variant="ghost"
                size="sm"
                disabled={isLoading}
                onClick={async () => {
                  try {
                    const client = await getClient(
                      credentialGetter,
                      "sans-api-v1",
                    );
                    await client.post("/workflow/copilot/cancel", {
                      cancel_token: questionCancelToken,
                      workflow_copilot_chat_id: workflowCopilotChatId,
                      source: "stop_button",
                    });
                    if (workflowCopilotChatId)
                      await loadChatInPlace(workflowCopilotChatId);
                  } catch {
                    toast({
                      title: "Could not cancel the question",
                      description: "Please try again.",
                      variant: "destructive",
                    });
                  }
                }}
              >
                Cancel question
              </Button>
            ) : null}
            {/*
            Instant-ack placeholder: fills the send→first-frame gap. isLoading +
            turnId===null is the whole gate — mutually exclusive with the live
            bubble below (turnId!==null), so the first frame swaps them in one
            render. The send-time reset (see handleSend) keeps turnId null on
            every send, including queued-then-drained follow-ups.
          */}
            {isLoading && !isLoadingHistory && narrative.turnId === null ? (
              <InstantAckPlaceholder />
            ) : null}
            {/*
            Bottom in-flight narrative bubble. Suppressed once the terminal
            RESPONSE has frozen the narrative into the latest AI message —
            otherwise the same turn would render twice.
          */}
            {narrative.turnId !== null && narrative.terminal === null && (
              <div
                className="flex flex-col gap-2"
                role="status"
                aria-live="polite"
              >
                <NarrativeView
                  turn={narrative}
                  onBlockSelect={onBlockSelect}
                  workingRowActive={showWorkingRow}
                />
                {!isLoadingHistory &&
                livePauseFrame &&
                livePauseFrame.turn_id === narrative.turnId ? (
                  <CredentialCard
                    frame={liveFrameToCardFrame(livePauseFrame)}
                    mode="inline-pause"
                    reloadKey={credentialsReloadKey}
                    resolvedOutcome={
                      credentialResolutions[livePauseFrame.turn_id]
                    }
                    // A picked credential (id + name from the fetched list) answers through the typed
                    // resume POST, which origin-binds; the Add-credential CTA (no id) opens the modal.
                    onConnect={(credentialId, name) =>
                      credentialId
                        ? void respondToCredentialPause(
                            livePauseFrame,
                            "connected",
                            credentialId,
                            name,
                          )
                        : openCredentialModal(
                            livePauseFrame,
                            livePauseFrame.turn_id,
                          )
                    }
                    onSkip={() =>
                      void respondToCredentialPause(livePauseFrame, "skip")
                    }
                  />
                ) : null}
              </div>
            )}
          </div>
        </div>
        {!isPinned ? (
          <button
            type="button"
            onClick={jumpToLatest}
            className="absolute bottom-4 left-1/2 flex -translate-x-1/2 items-center gap-1 rounded-full border border-border bg-slate-elevation3 px-3 py-1 text-xs text-foreground shadow-md hover:bg-slate-elevation4"
          >
            <ChevronDownIcon className="h-3 w-3" />
            Jump to latest
          </button>
        ) : null}
      </div>

      {/* Input */}
      <div className="border-t border-border p-3">
        {codeOptionAvailable ? (
          <div className="mb-2">
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  title="Switch mode"
                  aria-label="Switch mode"
                  className="flex items-center gap-1.5 rounded-full border border-border bg-slate-elevation2 px-2.5 py-1 text-[11px] font-medium text-muted-foreground hover:bg-slate-elevation3 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <span>Mode:</span>
                  <ModeGlyph glow={codeStateActive} />
                  <span className="text-foreground">
                    {codeStateActive ? "Build with code" : "Build"}
                  </span>
                  <ChevronDownIcon className="h-3 w-3" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent
                side="top"
                align="start"
                className="w-[272px] p-1.5"
                onCloseAutoFocus={(event) => event.preventDefault()}
              >
                {modeMenuItems}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        ) : null}
        {proposedWorkflow &&
        pendingProposalTurnId &&
        (gateOwnerIndex !== lastTurnIndex || isLoading) ? (
          <button
            type="button"
            onClick={() => {
              if (!pendingProposalTurnId) return;
              document
                .getElementById(`copilot-gate-${pendingProposalTurnId}`)
                ?.scrollIntoView({ behavior: "smooth", block: "center" });
              if (gateFlashTimer.current !== null) {
                clearTimeout(gateFlashTimer.current);
              }
              setGateFlashTurnId(pendingProposalTurnId);
              gateFlashTimer.current = setTimeout(() => {
                setGateFlashTurnId(null);
                gateFlashTimer.current = null;
              }, 1100);
            }}
            className="mb-2 flex items-center gap-1.5 rounded-full border border-border px-2.5 py-1 text-[10.5px] text-muted-foreground hover:bg-slate-elevation3"
          >
            <span className="h-1.5 w-1.5 rounded-full bg-sky-400" />1 proposal
            pending · Review
          </button>
        ) : null}
        {showWorkingRow ? (
          <CopilotWorkingStatus
            queued={Boolean(queuedPrompt)}
            onDismissQueued={restoreQueuedPromptToComposer}
          />
        ) : null}
        {!showWorkingRow &&
        queuedPrompt &&
        (queuedPrompt.reason === "working" || queuedBubbleOrphaned) ? (
          // Same state as the working row's queued pill, which a user on this
          // flag path also sees — so it takes the same tinted-pill grammar
          // rather than a bordered box, and the same edit glyph.
          <div className="mb-2 flex items-center gap-2 rounded-full bg-slate-400/[0.12] py-0.5 pl-2.5 pr-1 text-xs text-muted-foreground">
            <ReloadIcon className="h-3 w-3 shrink-0 animate-spin" />
            <span className="shrink-0 font-medium text-foreground">Queued</span>
            <span className="flex-1 truncate">{queuedPromptWaitingStatus}</span>
            <button
              type="button"
              onClick={restoreQueuedPromptToComposer}
              title="Edit queued message"
              aria-label="Edit queued message"
              className="shrink-0 rounded px-1 text-muted-foreground hover:text-accent-foreground"
            >
              <Pencil1Icon className="h-3 w-3" />
            </button>
          </div>
        ) : null}
        {inputStatusText && !showWorkingRow ? (
          <div
            className="mb-2 text-xs text-muted-foreground"
            aria-live="polite"
          >
            {inputStatusText}
          </div>
        ) : null}
        <SelectedBlockChip />
        <div className="flex items-end gap-1.5 rounded-lg border border-input bg-slate-elevation2 py-1.5 pl-3 pr-2.5 transition-colors focus-within:border-ring">
          <textarea
            ref={setTextareaRef}
            placeholder={composerPlaceholder({
              queuedPrompt: Boolean(queuedPrompt),
              isLoading,
              isWaitingForLiveBrowser,
              latestTurnIsAsk,
            })}
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={handleKeyPress}
            rows={1}
            className="min-h-10 flex-1 resize-none border-0 bg-transparent py-2 text-sm leading-6 text-foreground placeholder:truncate placeholder:text-muted-foreground focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
            style={{
              minHeight: "40px",
              maxHeight: "150px",
              overflowY: "hidden",
            }}
          />
          <SpeechInputButton
            isSupported={isSpeechSupported}
            isListening={isSpeechListening}
            isHearingSpeech={isSpeechHearing}
            onToggle={toggleSpeech}
            className="h-8 w-8 rounded-full border-0 bg-transparent"
            iconClassName="h-3.5 w-3.5"
          />
          <TooltipProvider>
            <ControlTooltip
              content={morphButtonLabel}
              blocked={waitingOnQueueOnly}
            >
              <button
                type="button"
                disabled={waitingOnQueueOnly || isStopping}
                aria-busy={isStopping}
                onClick={() =>
                  turnObservablyRunning && !hasComposerText
                    ? cancelSend("stop_button")
                    : handleSend()
                }
                aria-label={morphButtonLabel}
                className={cn(
                  "group/stop relative flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-lg transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring active:scale-[0.92] disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50",
                  showsStopGlyph
                    ? "bg-slate-elevation3"
                    : "bg-cta text-cta-foreground hover:bg-cta-hover",
                )}
              >
                {showsStopGlyph ? (
                  <>
                    <span
                      aria-hidden
                      data-testid="copilot-stop-orbit"
                      className={cn(
                        "absolute -inset-[50%] animate-copilot-stop-orbit motion-reduce:hidden",
                        isStopping && "paused",
                      )}
                      style={{
                        background: STOP_ORBIT_GRADIENT,
                        filter: "blur(1.5px)",
                        willChange: "transform",
                      }}
                    />
                    <span
                      aria-hidden
                      className="absolute inset-0 hidden bg-[rgba(150,195,255,0.55)] motion-reduce:block"
                    />
                    <span
                      aria-hidden
                      className="absolute inset-[2px] flex items-center justify-center rounded-md bg-slate-elevation3 transition-colors group-hover/stop:bg-slate-elevation5"
                    >
                      <span className="h-[11.5px] w-[11.5px] rounded-[2.3px] bg-foreground" />
                    </span>
                  </>
                ) : (
                  <ArrowUpIcon className="h-4 w-4" />
                )}
              </button>
            </ControlTooltip>
          </TooltipProvider>
        </div>
      </div>

      {/* Resize Handles */}
      {!docked && (
        <>
          {/* Corners */}
          <div
            className="absolute bottom-0 right-0 z-10 h-3 w-3 cursor-nwse-resize"
            onMouseDown={(e) => handleResizeMouseDown(e, "se")}
            title="Resize"
          />
          <div
            className="absolute bottom-0 left-0 z-10 h-3 w-3 cursor-nesw-resize"
            onMouseDown={(e) => handleResizeMouseDown(e, "sw")}
            title="Resize"
          />
          <div
            className="absolute right-0 top-0 z-10 h-3 w-3 cursor-nesw-resize"
            onMouseDown={(e) => handleResizeMouseDown(e, "ne")}
            title="Resize"
          />
          <div
            className="absolute left-0 top-0 z-10 h-3 w-3 cursor-nwse-resize"
            onMouseDown={(e) => handleResizeMouseDown(e, "nw")}
            title="Resize"
          />
          {/* Edges */}
          <div
            className="absolute left-3 right-3 top-0 z-10 h-1 cursor-ns-resize"
            onMouseDown={(e) => handleResizeMouseDown(e, "n")}
            title="Resize"
          />
          <div
            className="absolute bottom-0 left-3 right-3 z-10 h-1 cursor-ns-resize"
            onMouseDown={(e) => handleResizeMouseDown(e, "s")}
            title="Resize"
          />
          <div
            className="absolute bottom-3 left-0 top-3 z-10 w-1 cursor-ew-resize"
            onMouseDown={(e) => handleResizeMouseDown(e, "w")}
            title="Resize"
          />
          <div
            className="absolute bottom-3 right-0 top-3 z-10 w-1 cursor-ew-resize"
            onMouseDown={(e) => handleResizeMouseDown(e, "e")}
            title="Resize"
          />
        </>
      )}
      {/* Mounted only while open: CredentialsModal pulls react-query at mount,
          so keeping it mounted-but-closed would tax every render for nothing. */}
      {credentialModalOpen ? (
        <CredentialsModal
          isOpen
          // A sign-in pause always needs a password credential; force the form
          // so a lingering ?type=credit-card/secret param can't open the wrong one.
          overrideType={CredentialModalTypes.PASSWORD}
          // Seed tested_url from the pause frame's login page so a quick-add
          // credential matches later asks. Terminal frames carry no URL — the
          // field stays empty then.
          defaultTestUrl={
            pendingCredentialConnect.current?.frame?.login_page_urls?.[0]
          }
          onOpenChange={(open) => {
            setCredentialModalOpen(open);
            if (!open) pendingCredentialConnect.current = null;
          }}
          onCredentialCreated={handleCredentialCreated}
        />
      ) : null}
    </div>
  );

  if (docked) {
    return portalTarget ? createPortal(content, portalTarget) : null;
  }
  return content;
}
