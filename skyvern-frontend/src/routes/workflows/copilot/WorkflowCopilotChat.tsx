import type {
  CopilotAcceptSnapshot,
  EditorStateSnapshot,
  RestoreResult,
} from "../editor/editorStateSnapshot";
import { flushBufferedEditorEdits } from "@/hooks/useDeferredLockedEdit";
import { hashKey } from "@tanstack/react-query";
import {
  type AcceptAttempt,
  applyWroteNothing,
  type GateFailure,
  proposalTokenOf,
  definitiveAcceptRejection,
} from "./acceptFence";
import {
  beginCopilotAcceptance,
  readStoredAccept,
  storePendingAccept,
  reconcileYamlDraftAfterGraphChange,
  finishCopilotAcceptance,
  refuseMutationDuringYamlCommit,
  useWorkflowYamlEditorStore,
  withCopilotAcceptance,
} from "@/store/WorkflowYamlEditorStore";
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
import type { AxiosInstance, AxiosRequestConfig } from "axios";
import { getClient, deleteUploadedFileOnPageExit } from "@/api/AxiosClient";
import { queryClient } from "@/api/QueryClient";
import {
  ActionsApiResponse,
  type CredentialApiResponse,
  getReadableActionType,
} from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { CredentialsModal } from "@/routes/credentials/CredentialsModal";
import { CredentialModalTypes } from "@/routes/credentials/useCredentialModalState";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";
import { RecordingPanel } from "@/routes/workflows/editor/recording/RecordingPanel";
import {
  ReloadIcon,
  Cross2Icon,
  ChevronDownIcon,
  ArrowUpIcon,
  FileIcon,
  UploadIcon,
  PlusIcon,
  ExclamationTriangleIcon,
  EnterFullScreenIcon,
} from "@radix-ui/react-icons";
import { createPortal } from "react-dom";
import { stringify as convertToYAML } from "yaml";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import { useCopilotHeaderStore } from "@/store/useCopilotHeaderStore";
import {
  buildWorkflowCopilotContext,
  buildWorkflowYamlDocument,
  restoreWorkflowCopilotSettings,
} from "@/routes/workflows/editor/workflowYamlDocument";
import { apiWorkflowToSettings } from "@/routes/workflows/editor/apiWorkflowToSettings";
import { convert } from "@/routes/workflows/editor/workflowEditorUtils";
import { useWorkflowSnapshotStore } from "@/store/WorkflowSnapshotStore";
import {
  WorkflowApiResponse,
  WorkflowSettings,
} from "@/routes/workflows/types/workflowTypes";
import { describeRecordedAction } from "@/routes/workflows/workflowBlockUtils";
import {
  isBlockItem,
  WorkflowRunTimelineItem,
} from "@/routes/workflows/types/workflowRunTypes";
import { toast } from "@/components/ui/use-toast";
import { getSseClient } from "@/api/sse";
import {
  CopilotAttachedFile,
  WorkflowCopilotCancelRequest,
  CopilotProposalMetadata,
  CopilotProposalRunFacts,
  WorkflowCopilotCancelSource,
  WorkflowCopilotChatHistoryMessage,
  WorkflowCopilotMessageFeedback,
  WorkflowCopilotMessageFeedbackRating,
  WorkflowCopilotMessageFeedbackResponse,
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
  WorkflowCopilotCodegenProgressUpdate,
  WorkflowCopilotCredentialRequiredUpdate,
  WorkflowCopilotCredentialPauseResolvedUpdate,
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
import { AutoAcceptChip } from "./AutoAcceptChip";
import { SelectedBlockChip } from "./SelectedBlockChip";
import { readSelectedBlockLabel } from "./selectedBlockLabel";
import { selectAutoBoundReceiptIndexes } from "./autoBoundReceiptIndexes";
import { shouldWaitForLiveBrowser } from "./browserReadiness";
import {
  QueuedPromptReason,
  appendQueuedText,
  resolveDrainAction,
  resolveSendAction,
} from "./sendQueue";
import { shouldAutoApplyWorkflowResponse } from "./proposalDisposition";
import { InstantAckPlaceholder, NarrativeView } from "./NarrativeView";
import { CopilotMarkdown } from "./CopilotMarkdown";
import { FeedbackThumbs } from "@/components/feedback/FeedbackThumbs";
import { CopilotWorkingStatus } from "./CopilotWorkingStatus";
import { QueuedMessageStrip } from "./QueuedMessageStrip";
import {
  RecordingRefinementProgressCard,
  type RecordingRefinementStatus,
} from "./RecordingRefinementProgressCard";
import { useRunLifecycleAnnouncements } from "./useRunLifecycleAnnouncements";
import { useHistoryLoad } from "./useHistoryLoad";
import { ConfirmCard, shouldShowConfirmCard } from "./cards/ConfirmCard";
import { ConnectedAccountChoiceCard } from "./cards/ConnectedAccountChoiceCard";
import { QuestionPartsCard } from "./cards/QuestionPartsCard";
import { WorkPlanCard } from "./cards/WorkPlanCard";
import { nextAnsweringMessage, previousAskingMessage } from "./cardAdjacency";
import { composerPlaceholder } from "./composerPlaceholder";
import {
  ensureCredentialRecoveryToken,
  readCredentialRecoveryHistory as readCredentialRecoveryHistoryRequest,
  type CredentialRecoveryHistoryResponse,
} from "./credentialRecovery";
import { connectedAccountChoiceLabel } from "./cards/connectedAccountChoiceLabel";
import { shouldShowDiffCard } from "./cards/DiffCard";
import { ReviewGateCard, getReviewGateVerdict } from "./cards/ReviewGateCard";
import { GoogleReconnectCard } from "./cards/GoogleReconnectCard";
import {
  CredentialCard,
  type CredentialRequiredFrame,
  type CredentialRequiredReason,
  type CredentialPauseHistorical,
  UPDATE_ASK_REASONS,
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
  parseCredentialPause,
  parseUtcIsoMs,
} from "./narrativeState";
import { computeFollowSignature, useStickToBottom } from "./useStickToBottom";
import { useTurnActivityChange } from "./useTurnActivityChange";
import { useSpeechToTextField } from "@/hooks/useSpeechToTextField";
import { SpeechInputButton } from "@/components/SpeechInputButton";
import { cn, formatElapsedSeconds } from "@/util/utils";
import { ControlTooltip } from "@/routes/workflows/studio/ControlTooltip";
import { useSwitchStudioRun } from "@/routes/workflows/studio/runSwitchNavigation";
import { searchWithSystemBlockFocus } from "@/routes/workflows/editor/hooks/useSelectedBlockUrlSync";
import { studioPanelId } from "@/routes/workflows/studio/constants";
import {
  liveLocationState,
  liveSearch,
} from "@/routes/workflows/studio/liveSearch";
import { useStudioPanes } from "@/routes/workflows/studio/useStudioPanes";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useRecordingRefinementEvidenceStore } from "@/store/RecordingRefinementEvidenceStore";
import { useWorkflowBlockSearchStore } from "@/store/WorkflowBlockSearchStore";
import { resolveTimelineBlockJumpNodeId } from "@/routes/workflows/studio/runview/timelineBlockJump";
import { TooltipProvider } from "@/components/ui/tooltip";

// Cap on retained per-turn snap-back snapshots. A typical session has a
// handful of turns; this ceiling guards a runaway long-running chat.
const MAX_TURN_SNAPSHOTS = 20;
// A stream that closes with no terminal frame is usually a lost client
// connection while the server handler runs on to persist the real reply, so the
// ladder is sized to the server's own turn budget rather than to a short wait.
// How long Turn off waits for a chat's in-flight Accepts before giving up and reporting failure, so an
// apply that never answers cannot leave that chat's gate without its Accept actions. Accept p95 is ~11s.
export const ACCEPT_SETTLE_CEILING_MS = 30_000;

const requestTokenKey = (workflowId: string) =>
  `copilot-request-cancel-token:${workflowId}`;

function storedRequestCancelToken(
  workflowId: string | undefined,
): string | null {
  try {
    return workflowId
      ? sessionStorage.getItem(requestTokenKey(workflowId))
      : null;
  } catch {
    return null;
  }
}

async function readCredentialRecoveryHistory<
  T extends WorkflowCopilotChatHistoryResponse,
>(
  client: Pick<AxiosInstance, "get">,
  workflowId: string | undefined,
  config: AxiosRequestConfig,
  options?: { retryTransientFailure?: boolean },
): Promise<CredentialRecoveryHistoryResponse<T>> {
  let requestCancelToken = config.params?.request_cancel_token;
  let response = await readCredentialRecoveryHistoryRequest<T>(
    client,
    workflowId,
    config,
    options,
  );
  const token = storedRequestCancelToken(workflowId);
  if (
    !requestCancelToken &&
    token &&
    response.data.pending_credential_requests?.length &&
    response.data.workflow_copilot_chat_id
  ) {
    // Scope correlation to the displayed pause, so an older request cannot
    // change which chat an ordinary workflow-history read opens.
    response = await readCredentialRecoveryHistoryRequest<T>(
      client,
      workflowId,
      {
        ...config,
        params: {
          ...config.params,
          workflow_copilot_chat_id: response.data.workflow_copilot_chat_id,
          request_cancel_token: token,
        },
      },
      options,
    );
    requestCancelToken = token;
  }
  return {
    ...response,
    data: { ...response.data, request_cancel_token: requestCancelToken },
  };
}

async function requestWithin<T>(
  request: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      request(controller.signal),
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => {
          controller.abort();
          reject(new Error("Copilot request timed out"));
        }, ACCEPT_SETTLE_CEILING_MS);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

async function settledWithin(
  promise: Promise<unknown>,
  ms: number,
): Promise<boolean> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      promise.then(
        () => true,
        () => true,
      ),
      new Promise<boolean>((resolve) => {
        timer = setTimeout(() => resolve(false), Math.max(0, ms));
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

const RECOVERY_POLL_DELAYS_MS = [2_000, 3_000, 5_000, 8_000, 12_000, 20_000];
const RECOVERY_POLL_STEADY_MS = 30_000;
// The server's RECONCILE_ABANDON_AFTER_SECONDS is 1_320_000ms. The margin holds
// several 30s reads past it, so a turn whose reply was never persisted reaches
// both the read that produces its interrupted row and the later one that picks
// up the real reply if the turn finishes after all.
const RECOVERY_POLL_BUDGET_MS = 1_500_000;
const CANONICAL_READ_TIMEOUT_MS = 5_000;
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
// Must stay equal to REFINE_RECORDING_RECEIPT in routes/workflow_copilot.py: the server
// rewrites the message, so a different wording here would change the row on history reload.
const refineRecordingReceipt = (actionCount: number) =>
  `Refine the recording (${actionCount} actions) into a reusable workflow.`;
const REFINE_RECORDING_RECEIPT_PATTERN =
  /^Refine the recording \((\d+) actions\) into a reusable workflow\.$/;

function refineRecordingActionCount(content: string): number | null {
  const match = REFINE_RECORDING_RECEIPT_PATTERN.exec(content);
  if (!match) return null;
  const count = Number(match[1]);
  return Number.isSafeInteger(count) && count >= 0 ? count : null;
}

function isCancelledRefinementTurn(
  terminalReason: string | null | undefined,
  narrative: TurnNarrativeState | undefined,
): boolean {
  return (
    terminalReason === "cancel" ||
    terminalReason === "user_cancelled" ||
    narrative?.cancelled === true
  );
}

// diagnose_run and refine_recording both open the turn with a server-authored receipt.
const isProductAuthoredAction = (action: ArmedProductAction | null): boolean =>
  action?.action === "diagnose_run" || action?.action === "refine_recording";

type ArmedProductAction =
  | { action: "test_end_to_end"; workflowRunId?: undefined; nonce?: undefined }
  | { action: "diagnose_run"; workflowRunId: string; nonce?: undefined }
  | { action: "refine_recording"; workflowRunId?: undefined; nonce: string };

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
  kind?:
    | "run_lifecycle"
    | "status_notice"
    | "recording_refinement"
    | "initial_handoff";
  recoveryTurnId?: string;
  recordingRefinement?: {
    actionCount: number;
    startedAtMs: number;
    status: RecordingRefinementStatus;
    turnId?: string;
  };
  attachedFiles?: CopilotAttachedFile[];
  // Persisted row id, known after a history load or a feedback write; a live turn is rated by turnId.
  messageId?: string;
  feedback?: WorkflowCopilotMessageFeedback | null;
}

const VIDEO_ATTACHMENT_EXTENSIONS = [".mp4", ".webm", ".mov"] as const;
const ATTACHMENT_EXTENSIONS = [
  ".csv",
  ".xlsx",
  ".xls",
  ".pdf",
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".bmp",
  ".webp",
  ".tiff",
  ".tif",
  ...VIDEO_ATTACHMENT_EXTENSIONS,
] as const;
const ATTACHMENT_ACCEPT = ATTACHMENT_EXTENSIONS.join(",");
const ATTACHMENT_SIZE_LIMIT_BYTES = 10 * 1024 * 1024;
const VIDEO_ATTACHMENT_SIZE_LIMIT_BYTES = 30 * 1024 * 1024;
// Mirrors MAX_ATTACHED_FILES_PER_MESSAGE on the chat request.
const ATTACHMENT_COUNT_LIMIT = 20;

function isSupportedAttachment(file: File): boolean {
  const filename = file.name.toLowerCase();
  return ATTACHMENT_EXTENSIONS.some((extension) =>
    filename.endsWith(extension),
  );
}

function isVideoAttachment(file: File): boolean {
  const filename = file.name.toLowerCase();
  return VIDEO_ATTACHMENT_EXTENSIONS.some((extension) =>
    filename.endsWith(extension),
  );
}

function attachmentSizeLimit(file: File): number {
  return isVideoAttachment(file)
    ? VIDEO_ATTACHMENT_SIZE_LIMIT_BYTES
    : ATTACHMENT_SIZE_LIMIT_BYTES;
}

function hasFileDragPayload(dataTransfer: DataTransfer): boolean {
  return Array.from(dataTransfer.types).includes("Files");
}

function sameFileIds(turnFileIds: string[] | undefined, fileIds: string[]) {
  return (
    turnFileIds !== undefined &&
    turnFileIds.length === fileIds.length &&
    fileIds.every((fileId) => turnFileIds.includes(fileId))
  );
}

// An in-flight or failed upload has no file id yet, so it is tracked beside the resolved ones.
type PendingAttachment = {
  localId: string;
  filename: string;
  status: "uploading" | "error";
  error?: string;
};

function AttachmentChip({
  filename,
  available,
  status,
  error,
  onRemove,
}: {
  filename: string;
  available?: boolean;
  status?: "uploading" | "error";
  error?: string;
  onRemove?: () => void;
}) {
  const unusable = status === "error" || available === false;
  return (
    <span
      className={cn(
        "flex max-w-[220px] items-center gap-1.5 rounded-md border px-2 py-1 text-[11.5px]",
        unusable
          ? "border-destructive/40 text-destructive"
          : "border-white/10 bg-slate-elevation3 text-muted-foreground",
      )}
    >
      {status === "uploading" ? (
        <ReloadIcon className="h-3 w-3 shrink-0 animate-spin" />
      ) : unusable ? (
        <ExclamationTriangleIcon className="h-3 w-3 shrink-0" />
      ) : (
        <FileIcon className="h-3 w-3 shrink-0" />
      )}
      <span className="min-w-0 flex-1 truncate" title={filename}>
        {filename || "Unnamed file"}
      </span>
      {available === false ? (
        <span className="shrink-0 text-[10px]">no longer available</span>
      ) : null}
      {status === "error" && error ? (
        <span className="shrink-0 text-[10px]">{error}</span>
      ) : null}
      {onRemove ? (
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Remove ${filename}`}
          className="shrink-0 rounded p-0.5 hover:bg-accent hover:text-accent-foreground"
        >
          <Cross2Icon className="h-3 w-3" />
        </button>
      ) : null}
    </span>
  );
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

const ACCEPT_IN_FLIGHT_SAVE_REASON = "Copilot is saving your accepted changes.";

const ACCEPT_SAVED_NOT_SHOWN_SAVE_REASON =
  "Copilot saved this change but the editor couldn't load it, so saving now would write an older workflow over it.";

const ACCEPT_UNCONFIRMED_SAVE_REASON =
  "Copilot couldn't confirm whether an Accept already saved. Use Try again on the review gate first.";

const WORKFLOW_CLAIMED_SAVE_REASON =
  "Another change to this workflow is being saved right now. Reload the page before saving, so you don't overwrite it \u2014 reloading discards unsaved canvas edits.";

const PROPOSAL_CHANGED_SAVE_REASON =
  "This workflow changed after Copilot staged its proposal, so this canvas may be out of date. Reload the page before saving, so you don't overwrite that change \u2014 reloading discards unsaved canvas edits.";

const ACCEPT_STALE_CANVAS_SAVE_REASON =
  "Copilot couldn't confirm an earlier Accept, so this canvas may be out of date. Saving it could overwrite newer changes \u2014 reload the page first, which discards unsaved canvas edits.";
const getLatestDiffCardTurnIdFromHistory = (
  messages: WorkflowCopilotChatHistoryMessage[],
): string | null => {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    const narrative = hydrateHistoryNarrative(
      message?.narrative_payload,
      message?.turn_outcome,
    );
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

type RecordingSnapshot = Pick<
  WorkflowCopilotChatRequest,
  "recording_in_progress" | "recording_deleted_step_ids"
>;

const snapshotRecording = (): RecordingSnapshot => {
  const { isRecording, deletedStepIds } = useRecordingStore.getState();
  return {
    recording_in_progress: isRecording,
    recording_deleted_step_ids: isRecording ? deletedStepIds : [],
  };
};

// Only "typed" text is added to by a later send; a programmatic message (account choice, block
// rebuild, armed action) is replaced, and a product receipt has no text to edit.
type QueuedPromptOrigin = "typed" | "programmatic" | "product";

type QueuedPrompt = {
  origin: QueuedPromptOrigin;
  selectedConnectedAccountId?: string;
  id: string;
  content: string;
  reason: QueuedPromptReason;
  audioBlob?: Blob | null;
  idempotencyKey?: string;
  attachments?: CopilotAttachedFile[];
  recording?: RecordingSnapshot;
};

type SendOptions = {
  selectedConnectedAccountId?: string;
  queuedMessageId?: string;
  optimisticMessageId?: string;
  skipQueue?: boolean;
  deferReservation?: boolean;
  audioBlob?: Blob | null;
  idempotencyKey?: string;
  attachments?: CopilotAttachedFile[];
  // Taken when the message was queued; undefined reads the store at send time.
  recording?: RecordingSnapshot;
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
  | WorkflowCopilotCodegenProgressUpdate
  | WorkflowCopilotTitleUpdate
  | WorkflowCopilotCredentialRequiredUpdate
  | WorkflowCopilotCredentialPauseResolvedUpdate
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

// The persisted credentialPause carries no name, so a tab that received the live
// credential_pause_resolved frame names the receipt from it.
function historicalCredentialOutcome(
  turn: TurnNarrativeState,
  pauseCardResolutions: Record<string, CredentialPauseHistorical>,
): CredentialPauseHistorical | undefined {
  const pause = turn.credentialPause;
  if (!pause || pause.outcome === "declined") return undefined;
  const credentialId = pause.credentialId ?? undefined;
  const name = credentialId
    ? Object.values(pauseCardResolutions).find(
        (resolution) => resolution.credentialId === credentialId,
      )?.name
    : undefined;
  return { outcome: pause.outcome, credentialId, name };
}

type CredentialResolution = CredentialPauseHistorical & {
  name?: string;
  // Terminal connect auto-sent a "continue" turn — drives the receipt copy.
  continued?: boolean;
};

// Append a resolution under its key (a turn or a card), capping the map with oldest-eviction like
// the sibling per-turn maps (turnSnapshots/turnOwnedRunIds). delete-then-set
// re-inserts an existing key as newest so an active one isn't evicted.
function withCappedResolution(
  prev: Record<string, CredentialResolution>,
  key: string,
  value: CredentialResolution,
): Record<string, CredentialResolution> {
  const next = { ...prev };
  delete next[key];
  next[key] = value;
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
            {message.attachedFiles && message.attachedFiles.length > 0 ? (
              <div className="mb-2 flex flex-wrap justify-end gap-1.5">
                {message.attachedFiles.map((attached) => (
                  <AttachmentChip
                    key={attached.file_id}
                    filename={attached.filename}
                    available={attached.available}
                  />
                ))}
              </div>
            ) : null}
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
      <div className="group/turn flex flex-col gap-2">
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

function RecordingChapterProxy({
  actionCount,
  newActionCount,
  lastActionTitle,
  isFinishing,
  previewOpen,
  onPreviewOpenChange,
  onJumpToChapter,
  onExpand,
}: {
  actionCount: number;
  newActionCount: number;
  lastActionTitle: string | null;
  isFinishing: boolean;
  previewOpen: boolean;
  onPreviewOpenChange: (open: boolean) => void;
  onJumpToChapter: () => void;
  onExpand: () => void;
}) {
  return (
    <div className="absolute inset-x-2 top-2 z-20 overflow-hidden rounded-lg border border-border bg-slate-elevation2 shadow-lg">
      <div className="flex items-center gap-2 px-2.5 py-2">
        <span className="flex size-6 shrink-0 items-center justify-center rounded-md bg-red-500/10">
          {isFinishing ? (
            <ReloadIcon className="size-3.5 animate-spin text-red-500 motion-reduce:animate-none" />
          ) : (
            <span className="size-2 animate-pulse rounded-full bg-red-500 motion-reduce:animate-none" />
          )}
        </span>
        <button
          type="button"
          onClick={onJumpToChapter}
          className="min-w-0 flex-1 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <span className="block truncate text-xs font-semibold text-foreground">
            {isFinishing
              ? "Finishing recording…"
              : "Copilot is following along"}
          </span>
          <span className="block truncate text-[10.5px] text-muted-foreground">
            {isFinishing ? (
              "Saving the last recorded actions"
            ) : (
              <>
                {actionCount} captured action{actionCount === 1 ? "" : "s"}
                {newActionCount > 0 ? ` · ${newActionCount} new` : ""}
              </>
            )}
          </span>
        </button>
        <button
          type="button"
          aria-label={
            previewOpen
              ? "Collapse recording preview"
              : "Expand recording preview"
          }
          onClick={() => onPreviewOpenChange(!previewOpen)}
          className="flex size-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <ChevronDownIcon
            className={cn(
              "size-3.5 transition-transform",
              previewOpen && "rotate-180",
            )}
          />
        </button>
        <button
          type="button"
          aria-label="Expand recording"
          onClick={onExpand}
          className="flex size-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <EnterFullScreenIcon className="size-3.5" />
        </button>
      </div>
      {previewOpen ? (
        <button
          type="button"
          onClick={onJumpToChapter}
          className="flex w-full items-center gap-2 border-t border-border px-3 py-2 text-left text-[11px] text-muted-foreground hover:bg-slate-elevation3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
        >
          <span className="size-1.5 shrink-0 rounded-full bg-red-500" />
          <span className="truncate">
            {lastActionTitle ?? "Waiting for the next browser action…"}
          </span>
        </button>
      ) : null}
    </div>
  );
}

// `persisted` true = atomic accept (server already wrote new version); false/undefined = local edit.
// `applied` marks a turn's accepted terminal apply; drafts and snap-backs omit it.
export type WorkflowUpdateOptions = {
  persisted?: boolean;
  keepLocalGraph?: boolean;
  applied?: boolean;
  settings?: WorkflowSettings;
  // A mid-turn draft lands while the user may be renaming the agent, so its title is
  // applied only if nothing has named it yet. Discrete applies (accept, snap-back)
  // are authoritative and keep the force path.
  midTurnDraft?: boolean;
  // This workflow was read from the server IN THE SAME ACT as the apply, so the canvas it
  // installs is current. `persisted` does NOT imply this: it also marks a workflow held from an
  // earlier read and applied later, which is current only if nothing has written since. Opt-in
  // rather than opt-out, so a caller that cannot make the claim makes it by saying nothing.
  fresh?: boolean;
};

interface WorkflowCopilotChatProps {
  captureEditorState?: () => EditorStateSnapshot | null;
  restoreEditorState?: (snapshot: EditorStateSnapshot) => RestoreResult;
  onWorkflowPersisted?: (workflowPermanentId: string) => void;
  onWorkflowUpdate?: (
    workflow: WorkflowApiResponse,
    options?: WorkflowUpdateOptions,
  ) => void;
  onReviewWorkflow?: (
    workflow: WorkflowApiResponse,
    clearPending: () => void,
    reject: () => Promise<boolean>,
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
  /** Files uploaded before the handoff; sent with the initial message. */
  initialAttachments?: Array<CopilotAttachedFile>;
  initialAction?: CopilotProductAction;
  onInitialMessageConsumed?: () => void;
  onUploadSOP?: (file: File) => void;
  canUploadSOP?: boolean;
  isUploadingSOP?: boolean;
  onRecordTask?: () => void;
  canRecordTask?: boolean;
  authoringUnavailableReason?: string | null;
  // Render as a docked panel (no float/drag/resize) instead of a floating window.
  docked?: boolean;
  // Render frameless — no border, background, or title; the header keeps only
  // the controls row. Only used when `docked`.
  chromeless?: boolean;
  // When docked, render into this element via a portal (keeps the component in
  // its parent's React tree so canvas callbacks stay wired) instead of inline.
  portalTarget?: HTMLElement | null;
}

type CopilotEditorStateSnapshot = EditorStateSnapshot & {
  yamlEditor: Pick<
    ReturnType<typeof useWorkflowYamlEditorStore.getState>,
    "active" | "draft" | "entrySnapshot" | "stale" | "error"
  >;
};

function captureCopilotEditorState(
  captureEditorState: WorkflowCopilotChatProps["captureEditorState"],
): CopilotEditorStateSnapshot | null {
  const snapshot = captureEditorState?.();
  if (!snapshot) return null;
  const { active, draft, entrySnapshot, stale, error } =
    useWorkflowYamlEditorStore.getState();
  return {
    ...snapshot,
    yamlEditor: { active, draft, entrySnapshot, stale, error },
  };
}

// Snap-back state keyed by turn_id so rapid resubmits don't clobber a prior
// turn's snapshot before its terminal frame lands. The snapshot captures
// pre-submit canvas state (including unsaved local edits) so Reject / Cancel /
// ERROR can revert exactly what the user submitted.
interface TurnSnapshot {
  snapshot: CopilotEditorStateSnapshot | null;
  workflowPersisted?: boolean;
  titlePersisted?: boolean;
  settings: WorkflowSettings | undefined;
  hadStagedDraft: boolean;
}

type RecoveryPollOwnership = {
  chatId: string | null;
  turnId: string | null;
  requestId: string;
  reservation?: symbol;
  canonicalRecovery: CanonicalRecovery | null;
  ownsTurn: boolean;
  recordingRefinementMessageId?: string;
  aliases: string[];
};

type RecoveryPoll = {
  stop: () => void;
  retry?: () => void;
  isReserved?: () => boolean;
  canAdopt?: (chatId: string | null) => boolean;
  retire?: (successor: RecoveryPoll) => RecoveryPollOwnership | undefined;
  adopt?: (
    chatId: string | null,
    turnId: string | null,
    options?: {
      requestId: string;
      reservation?: symbol;
      ownsTurn: boolean;
      recordingRefinementMessageId?: string;
    },
  ) => boolean;
};

type AcceptGatePresentation =
  | Exclude<GateFailure, { kind: "recover" }>
  | ({ kind: "recover" } & AcceptAttempt);

type CanonicalRecovery = {
  poll?: {
    chatId: string | null;
    turnId: string | null;
    requestId: string;
    deadline: number;
    ownsTurn: boolean;
    recordingRefinementMessageId?: string;
  };
  resumed?: boolean;
  cancellationRequested?: boolean;
  definitiveRejection?: string;
  retryAccept?: boolean;
  retryAcceptAvailable?: boolean;
  workflowPermanentId: string;
  preservedSettings?: WorkflowSettings;
  baseline?: WorkflowApiResponse;
  awaitingTurnId?: string;
  terminalConfirmed?: boolean;
  chatId?: string | null;
  acceptChatId?: string;
  gateAttempt?: AcceptAttempt;
  unattributedClaim?: boolean;
  savedWorkflow?: WorkflowApiResponse;
  savedOwnerTurnId?: string | null;
  acceptAttempt?: Pick<
    CopilotProposalMetadata,
    "owner_turn_id" | "revision" | "disposition"
  > | null;

  hadLocalEdits?: boolean;
  hadGraphEdits?: boolean;
  localDraftConflict?: boolean;
  draftChoice?: "keep" | "discard";
  retryApply?: () => Promise<boolean>;
  rollback?: TurnSnapshot;
  restoreRollback?: boolean;
  waitingForUnlock: boolean;
  yaml: { draft: string; entrySnapshot: string } | null;
};

function renderOutputValue(value: unknown): React.ReactNode {
  if (value === null || value === undefined || value === "") {
    return <span>—</span>;
  }
  if (typeof value === "string") {
    return (
      <span className="whitespace-pre-wrap [overflow-wrap:anywhere]">
        {value}
      </span>
    );
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return <span>—</span>;
    }
    return (
      <div className="ml-3">
        {value.map((item, index) => (
          <div key={index}>
            <span className="font-medium">{index + 1}.</span>{" "}
            {renderOutputValue(item)}
          </div>
        ))}
      </div>
    );
  }
  if (typeof value === "object") {
    const entries = Object.entries(value);
    if (entries.length === 0) {
      return <span>—</span>;
    }
    return (
      <div className="ml-3">
        {entries.map(([key, item]) => (
          <div key={key}>
            <span className="font-medium">{key}:</span>{" "}
            {renderOutputValue(item)}
          </div>
        ))}
      </div>
    );
  }
  return <span>{String(value)}</span>;
}

function ProposalRunFactsLine({ facts }: { facts: CopilotProposalRunFacts }) {
  if (!facts.available) {
    return (
      <p
        className="text-xs text-muted-foreground"
        data-testid="proposal-run-facts"
      >
        Associated test run unavailable. No other run was substituted.
      </p>
    );
  }
  return (
    <div
      className="space-y-1 text-xs text-muted-foreground"
      data-testid="proposal-run-facts"
    >
      <p>Associated test: {facts.status ?? "status unavailable"}</p>
      {facts.failure_reason ? <p>{facts.failure_reason}</p> : null}
      {facts.outputs.map((output) => (
        <div key={output.output_parameter_id}>
          <span className="font-medium text-foreground">
            {output.output_parameter_id}:
          </span>{" "}
          {renderOutputValue(output.value)}
        </div>
      ))}
    </div>
  );
}

// eslint-disable-next-line react-refresh/only-export-components -- Recovery survives routed editors.
export const canonicalRecoveriesByWorkflow = new Map<
  string,
  CanonicalRecovery
>();

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
  captureEditorState,
  restoreEditorState,
  onWorkflowPersisted,
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
  initialAttachments,
  initialAction,
  onInitialMessageConsumed,
  onUploadSOP,
  canUploadSOP = true,
  isUploadingSOP = false,
  onRecordTask,
  canRecordTask = false,
  authoringUnavailableReason,
  docked = false,
  chromeless = false,
  portalTarget,
}: WorkflowCopilotChatProps = {}) {
  const workflowPermanentId = useWorkflowPermanentId();
  useLayoutEffect(() => {
    if (workflowPermanentId)
      useWorkflowTitleStore
        .getState()
        .startCopilotMetadata(workflowPermanentId);
  }, [workflowPermanentId]);
  const outstandingAccept = useWorkflowYamlEditorStore((state) =>
    workflowPermanentId ? state.pendingAccepts[workflowPermanentId] : undefined,
  );
  const unattributedClaim =
    (outstandingAccept?.snapshot as CanonicalRecovery | undefined)
      ?.unattributedClaim === true;
  const initialHandoffMessageId = `initial-copilot-message-${workflowPermanentId ?? "pending"}`;
  const sopFileInputRef = useRef<HTMLInputElement>(null);
  const recordingAuthoringActive = useRecordingStore(
    (state) => state.isRecording || state.finishRequested || state.isCommitting,
  );
  const recordingIsFinishing = useRecordingStore(
    (state) => state.finishRequested || state.isCommitting,
  );
  // Recording is deliberately conversational: the live chapter owns capture,
  // while the existing composer remains available for instructions and
  // clarifications. SOP upload and finishing a recording own it exclusively.
  const authoringInProgress = isUploadingSOP || recordingIsFinishing;
  const [messages, setMessages] = useState<ChatMessage[]>(() =>
    !initialAction && initialMessage
      ? [
          {
            id: initialHandoffMessageId,
            sender: "user",
            content: initialMessage,
            kind: "initial_handoff",
            attachedFiles: initialAttachments,
          },
        ]
      : [],
  );
  const [workPlan, setWorkPlan] = useState<string[]>([]);
  const [proposedWorkflow, setProposedWorkflow] =
    useState<WorkflowApiResponse | null>(null);
  const [pendingProposalMetadata, setPendingProposalMetadata] =
    useState<CopilotProposalMetadata | null>(null);
  const [pendingProposalRun, setPendingProposalRun] =
    useState<CopilotProposalRunFacts | null>(null);
  // Owning turn of the current proposedWorkflow. Kept alongside it (never
  // merged into one object) so the gate can re-attach to its owning message.
  const [pendingProposalTurnId, setPendingProposalTurnId] = useState<
    string | null
  >(null);
  // What the pending gate reports after a failed Accept or proposal reload;
  // its Retry re-runs that call.
  const [gateFailure, setGateFailure] = useState<AcceptGatePresentation | null>(
    null,
  );
  const [isAccepting, setIsAccepting] = useState(false);
  const [startupReadyFor, setStartupReadyFor] = useState<string | null>(null);
  const [startupFailed, setStartupFailed] = useState(false);
  const [startupRetry, setStartupRetry] = useState(0);
  const startupPending = Boolean(
    workflowPermanentId && startupReadyFor !== workflowPermanentId,
  );
  const deferredStartupClaim = useRef<{
    workflowPermanentId: string;
    row: WorkflowCopilotChatHistoryResponse;
  } | null>(null);
  const acceptHoldReason = startupPending
    ? "Checking saved Copilot changes. Retry if the check fails."
    : isAccepting
      ? ACCEPT_IN_FLIGHT_SAVE_REASON
      : outstandingAccept
        ? unattributedClaim
          ? WORKFLOW_CLAIMED_SAVE_REASON
          : gateFailure?.kind === "saved"
            ? ACCEPT_SAVED_NOT_SHOWN_SAVE_REASON
            : gateFailure?.kind === "changed"
              ? PROPOSAL_CHANGED_SAVE_REASON
              : ACCEPT_UNCONFIRMED_SAVE_REASON
        : null;
  const reloadCardShowing =
    gateFailure?.kind === "reload" && Boolean(proposedWorkflow);
  const saveHoldReason =
    acceptHoldReason ??
    (reloadCardShowing ? ACCEPT_STALE_CANVAS_SAVE_REASON : null);
  useLayoutEffect(() => {
    if (!saveHoldReason) return;
    const { setSaveBlockedReason } = useWorkflowHasChangesStore.getState();
    setSaveBlockedReason(saveHoldReason);
    return () => setSaveBlockedReason(null);
  }, [saveHoldReason]);
  const acceptUnresolved = acceptHoldReason !== null;
  const restoreAcceptFromHistory = useRef<
    (row: WorkflowCopilotChatHistoryResponse) => void
  >(() => {});
  useEffect(
    () =>
      useWorkflowYamlEditorStore.subscribe(() => {
        const deferred = deferredStartupClaim.current;
        const state = useWorkflowYamlEditorStore.getState();
        if (
          deferred &&
          deferred.workflowPermanentId === workflowPermanentId &&
          (!state.editorOwner ||
            state.editorOwner.workflowPermanentId === workflowPermanentId) &&
          !state.commitInProgress &&
          !state.authoringInProgress &&
          !state.copilotAcceptance
        ) {
          deferredStartupClaim.current = null;
          restoreAcceptFromHistory.current(deferred.row);
          if (!deferredStartupClaim.current)
            setStartupReadyFor(workflowPermanentId ?? null);
        }
      }),
    [workflowPermanentId],
  );
  // Bumped by every send, so a plain proposal reload started before a newer turn
  // cannot apply its older row over that turn's proposal.
  const sendEpochRef = useRef(0);
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
  // A running turn's stream handler reads this, so Turn off reaches the turn already in flight.
  const autoAcceptRef = useRef(autoAccept);
  useEffect(() => {
    autoAcceptRef.current = autoAccept;
  }, [autoAccept]);
  // Counts the user's own auto-accept writes, so a chat-row read that started before one cannot undo it.
  const autoAcceptWrites = useRef(0);
  // Accepts still running, per chat. Each apply writes its chat's auto_accept when it lands, so that chat's
  // Turn off must go after it; another chat's Accept cannot write it and must not hold it back.
  const acceptsInFlight = useRef(new Map<string, Promise<void>>());
  // How many Turn offs are in flight per chat. Their review gates offer no Accept until every one finishes, so
  // no Accept can start after a disable and write auto_accept back on. Counted, not a flag: one chat's Turn off
  // must not free another's, and a chip that remounts on a chat switch must not free the request still running.
  const [turningOffCounts, setTurningOffCounts] = useState<
    ReadonlyMap<string, number>
  >(() => new Map());
  const noteAutoAcceptWrite = () => {
    autoAcceptWrites.current += 1;
  };
  const setAutoAcceptFromWrite = (value: boolean) => {
    noteAutoAcceptWrite();
    setAutoAccept(value);
  };
  const [inputValue, setInputValue] = useState("");
  const [attachments, setAttachments] = useState<CopilotAttachedFile[]>([]);
  // A file returned to the tray after a failed send may already be saved on that message, so
  // removing its chip must not delete it; only a never-posted upload is safe to delete.
  const postedFileIds = useRef<Set<string>>(new Set());
  // Files whose chat request is on the wire but not yet confirmed; a same-tick duplicate can hold the
  // same ids, so nothing reached through that duplicate may delete them until the request settles.
  const inFlightFileIds = useRef<Set<string>>(new Set());
  // Files whose page-exit delete was issued and has not been answered yet, plus whether the page is
  // currently hidden: an upload that lands while hidden has no chip anyone can act on.
  const reclaimingFiles = useRef<Map<string, CopilotAttachedFile>>(new Map());
  const pageHiddenRef = useRef(false);
  const attachmentsRef = useRef<CopilotAttachedFile[]>([]);
  attachmentsRef.current = attachments;
  // Files a send has taken out of the tray but not yet posted; only unmount cleanup can reach them.
  // One entry per send that has taken files out of the tray and not yet posted them. New chat can
  // abort a send mid-preflight and let the next one start, so several can be waiting at once.
  const unpostedSendsRef = useRef<Set<CopilotAttachedFile[]>>(new Set());
  const composerMountedRef = useRef(true);
  const returnFilesToTray = useCallback((files: CopilotAttachedFile[]) => {
    // A file whose page-exit delete is on the wire, or already done, must not come back: its chip
    // could never be removed again, since the second delete answers 404.
    const restorable = files.filter(
      (item) => !reclaimingFiles.current.has(item.file_id),
    );
    if (restorable.length === 0) return;
    setAttachments((current) => {
      const known = new Set(current.map((item) => item.file_id));
      return [
        ...current,
        ...restorable.filter((item) => !known.has(item.file_id)),
      ];
    });
  }, []);
  const [pendingAttachments, setPendingAttachments] = useState<
    PendingAttachment[]
  >([]);
  const pendingAttachmentsRef = useRef<PendingAttachment[]>([]);
  pendingAttachmentsRef.current = pendingAttachments;
  const attachmentInputRef = useRef<HTMLInputElement>(null);
  const fileDragDepthRef = useRef(0);
  const [isFileDragging, setIsFileDragging] = useState(false);
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
  const { isLoadingHistory, beginHistoryLoad, endHistoryLoad } =
    useHistoryLoad();
  // Active mid-build credential pause frame for the in-flight turn. Cleared at
  // turn_start and at every terminal so a dead resume_token can never render.
  const [livePauseFrame, setLivePauseFrame] =
    useState<WorkflowCopilotCredentialRequiredUpdate | null>(null);
  const [recoveredPauseFrames, setRecoveredPauseFrames] = useState<
    WorkflowCopilotCredentialRequiredUpdate[]
  >([]);
  // Local credential-card resolutions keyed by turn_id: live pauses after a
  // successful resume POST, and terminal-mode connect/skip (which never POST).
  // name is captured at connect so the receipt keeps showing it after the turn
  // goes terminal and the live frame (and its matching list) is gone.
  const [credentialResolutions, setCredentialResolutions] = useState<
    Record<string, CredentialResolution>
  >({});
  // Live pause answers keyed by resume_token: one turn can raise a pick card and then an update card.
  const [pauseCardResolutions, setPauseCardResolutions] = useState<
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
    editingCredential?: CredentialApiResponse;
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
  // Composer text a send just took. A second Enter from a stale closure (same tick, or while
  // dictation finalizes) still reads it, and would queue or append it a second time. Held while
  // the composer stays empty, since only a stale closure can offer that text then.
  const consumedComposerTextRef = useRef<string | null>(null);
  useEffect(() => {
    if (inputValue !== "") consumedComposerTextRef.current = null;
  }, [inputValue]);
  // What the turn was asked to do. completedNormally starts false and is set
  // only on a clean terminal, so every other exit drains the queue as before.
  const lastTurnRef = useRef<{
    content: string;
    workflowPermanentId: string | undefined;
    hadAudio: boolean;
    hadBlockTarget: boolean;
    browserSessionId: string | null;
    attachmentIds: string[];
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
  const recoveryPolls = useRef(new Map<string, RecoveryPoll>());
  const chatPresentationGeneration = useRef(0);
  const recoverySnapshotStamps = useRef(new Map<string, number>());
  const recoveryGeneration = useRef(0);
  const recoveryCancelTokens = useRef(new Map<string, string>());
  const rememberRecoveryCancelTokens = useCallback(
    (data: WorkflowCopilotChatHistoryResponse) => {
      if (
        data.request_turn_id &&
        "request_cancel_token" in data &&
        typeof data.request_cancel_token === "string"
      )
        recoveryCancelTokens.current.set(
          data.request_turn_id,
          data.request_cancel_token,
        );
      for (const message of data.chat_history) {
        const outcome = message.turn_outcome;
        if (
          outcome?.copilot_turn_id &&
          "request_cancel_token" in outcome &&
          typeof outcome.request_cancel_token === "string"
        ) {
          recoveryCancelTokens.current.set(
            outcome.copilot_turn_id,
            outcome.request_cancel_token,
          );
        }
      }
      for (const question of data.question_interactions ?? []) {
        if (question.status === "pending" && data.pending_question_cancel_token)
          recoveryCancelTokens.current.set(
            question.turn_id,
            data.pending_question_cancel_token,
          );
      }
    },
    [],
  );
  const copilotReservation = useRef<symbol | null>(null);
  const turnOwner = useRef<{
    reservation: symbol;
    generation: number;
    editor: ReturnType<
      typeof useWorkflowYamlEditorStore.getState
    >["editorOwner"];
  } | null>(null);
  const isCopilotOwnerCurrent = useCallback((reservation: symbol) => {
    const owner = turnOwner.current;
    const state = useWorkflowYamlEditorStore.getState();
    return Boolean(
      composerMountedRef.current &&
      owner?.reservation === reservation &&
      owner.generation === recoveryGeneration.current &&
      owner.editor === state.editorOwner &&
      (!owner.editor || owner.editor.active),
    );
  }, []);
  const isCopilotTurnCurrent = useCallback(
    (reservation: symbol) =>
      isCopilotOwnerCurrent(reservation) &&
      useWorkflowYamlEditorStore.getState().copilotAcceptance === reservation,
    [isCopilotOwnerCurrent],
  );
  const editorSnapshotCaptureRef = useRef(captureEditorState);
  useLayoutEffect(() => {
    editorSnapshotCaptureRef.current = captureEditorState;
  }, [captureEditorState]);
  const reserveCopilotTurn = useCallback(
    (action?: "send" | "accept") => {
      const editor = useWorkflowYamlEditorStore.getState().editorOwner;
      if (
        !composerMountedRef.current ||
        (editor &&
          (!editor.active ||
            editor.workflowPermanentId !== workflowPermanentId))
      )
        return null;
      if (action) {
        useWorkflowYamlEditorStore.getState().flushDraft?.();
        flushBufferedEditorEdits();
      }
      const snapshot =
        action === "send"
          ? captureCopilotEditorState(editorSnapshotCaptureRef.current)
          : null;
      const reservation = beginCopilotAcceptance();
      if (!reservation) return null;
      if (action === "send") pendingSubmitSnapshot.current = snapshot;
      copilotReservation.current = reservation;
      turnOwner.current = {
        reservation,
        generation: recoveryGeneration.current,
        editor,
      };
      return reservation;
    },
    [workflowPermanentId],
  );
  const pendingCanonicalRecovery = useRef<CanonicalRecovery | null>(null);
  const captureLiveTurnRecovery = useRef<
    (() => CanonicalRecovery | null) | null
  >(null);
  const canonicalRecoveryInFlight = useRef(false);
  const canonicalRecoveryAbort = useRef<AbortController | null>(null);
  const [recoveryControls, setRecoveryControls] = useState<{
    retry: () => void;
    reject: () => void;
    reload?: () => void;
    cancelling?: boolean;
    conflict?: { keep: () => void; discard: () => void };
  } | null>(null);
  // A turn resumed from saved human input has no live SSE controller, but it
  // still owns the composer until history proves that continuation finished.
  const recoveredTurnOwnerRef = useRef<string | null>(null);
  // The poll is declared before the reconcile it calls on a recovered row.
  const reconcileCanonicalWorkflowRef = useRef<
    | ((
        reservation?: symbol,
        signal?: AbortSignal,
      ) => Promise<boolean | undefined>)
    | null
  >(null);
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
  const pendingSubmitSnapshot = useRef<CopilotEditorStateSnapshot | null>(null);
  const pendingSubmitSettings = useRef<WorkflowSettings | undefined>();
  const proposalPreservedSettings = useRef<{
    workflow: WorkflowApiResponse;
    settings: WorkflowSettings | undefined;
  } | null>(null);
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
  // The run this turn pointed the studio's Browser pane at; written once per
  // run and kept after it finishes so the pane can show that run's replay.
  const focusedTurnRunId = useRef<string | null>(null);
  // Build-follow: while a docked turn streams, the canvas follows the block the
  // copilot is working on. Any pointer press outside the copilot pane hands
  // control back to the user for the rest of the turn (log-tail semantics).
  const buildFollowEngaged = useRef(false);
  const lastFollowedLabelRef = useRef<string | null>(null);
  // Focusing the turn's run is the copilot acting for the user, not a
  // navigation they asked for, so it must not add a Back step.
  const { resolveLivePanes } = useStudioPanes();
  const switchStudioRun = useSwitchStudioRun({
    replace: true,
    systemFocus: true,
  });
  useEffect(() => {
    workflowCopilotChatIdRef.current = workflowCopilotChatId;
  }, [workflowCopilotChatId]);
  // workflowCopilotChatIdRef is assigned by the effect above, so it names the OLD chat until a
  // commit after navigation starts. Anything comparing it can therefore pass in that window.
  // This counter moves the instant a user navigates, the way sendEpochRef does for sends, so an
  // in-flight write can tell that the ground moved without waiting for the ref to catch up.
  const chatNavEpochRef = useRef(0);
  // A SEPARATE counter, because this answers a DIFFERENT QUESTION. chatNavEpochRef answers "did
  // we actually navigate", and it moves when a hydration APPLIES a different chat - so a
  // navigation that FAILS correctly leaves it alone and an in-flight Reject still completes.
  // This one answers "is there a newer user selection", which must move the instant the user
  // selects, whether or not that load ever succeeds. One counter cannot answer both: advancing
  // the navigation counter on selection would abandon a Reject over a navigation that never
  // happened.
  const chatSelectionEpochRef = useRef(0);
  // The chat id the last hydration actually applied. Maintained here rather than read back from
  // workflowCopilotChatIdRef, which lags by a commit - comparing against a lagging value reports
  // a change every time hydration runs twice before the effect catches up.
  const hydratedChatIdRef = useRef<string | null>(null);
  useLayoutEffect(() => {
    const activePolls = actionPollRef.current;
    const activeRecoveryPolls = recoveryPolls.current;
    composerMountedRef.current = true;
    setIsLoading(false);
    return () => {
      composerMountedRef.current = false;
      const pending =
        pendingCanonicalRecovery.current ?? captureLiveTurnRecovery.current?.();
      captureLiveTurnRecovery.current = null;
      if (pending && !pending.acceptChatId) {
        canonicalRecoveriesByWorkflow.delete(pending.workflowPermanentId);
        canonicalRecoveriesByWorkflow.set(pending.workflowPermanentId, {
          ...pending,
          retryApply: undefined,
          resumed: true,
        });
        void queryClient.invalidateQueries({
          queryKey: ["workflow", pending.workflowPermanentId],
          exact: true,
          refetchType: "none",
        });
        while (canonicalRecoveriesByWorkflow.size > MAX_TURN_SNAPSHOTS) {
          const oldest = canonicalRecoveriesByWorkflow.keys().next().value;
          if (oldest === undefined) break;
          canonicalRecoveriesByWorkflow.delete(oldest);
        }
      }
      turnOwner.current = null;
      streamingAbortController.current?.abort();
      streamingAbortController.current = null;
      inFlightRef.current = false;
      canonicalRecoveryAbort.current?.abort();
      canonicalRecoveryAbort.current = null;
      canonicalRecoveryInFlight.current = false;
      pendingCanonicalRecovery.current = null;
      if (copilotReservation.current) {
        if (
          !Object.values(
            useWorkflowYamlEditorStore.getState().pendingAccepts,
          ).some((item) => item.reservation === copilotReservation.current)
        )
          finishCopilotAcceptance(copilotReservation.current);
        copilotReservation.current = null;
      }
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
      activeRecoveryPolls.forEach((poll) => poll.stop());
      activeRecoveryPolls.clear();
    };
  }, [workflowPermanentId]);
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
  const initialMessageIdentityRef = useRef({
    workflowPermanentId,
    initialMessage,
  });
  useEffect(() => {
    const previousIdentity = initialMessageIdentityRef.current;
    const workflowChanged =
      previousIdentity.workflowPermanentId !== workflowPermanentId;
    const messageChanged = previousIdentity.initialMessage !== initialMessage;
    initialMessageIdentityRef.current = { workflowPermanentId, initialMessage };

    if (
      !initialAction &&
      initialMessage &&
      (workflowChanged || messageChanged)
    ) {
      setMessages((current) => [
        {
          id: initialHandoffMessageId,
          sender: "user",
          content: initialMessage,
          kind: "initial_handoff",
          attachedFiles: initialAttachments,
        },
        ...current.filter((message) => message.kind !== "initial_handoff"),
      ]);
    } else if (workflowChanged) {
      setMessages((current) =>
        current.filter((message) => message.kind !== "initial_handoff"),
      );
    }
  }, [
    initialAction,
    initialAttachments,
    initialHandoffMessageId,
    initialMessage,
    workflowPermanentId,
  ]);
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
        editorOpen: resolveLivePanes().includes("editor"),
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
      resolveLivePanes,
    ],
  );
  const recoverCredentialTurn = useRef<
    (chatId: string, turnId: string) => boolean
  >(() => false);
  const respondToCredentialPause = useCallback(
    async (
      frame: WorkflowCopilotCredentialRequiredUpdate,
      action: "connected" | "skip",
      credentialId?: string,
      name?: string,
    ) => {
      if (credentialResponseInFlight.current) return;
      if (
        !streamingAbortController.current &&
        !recoverCredentialTurn.current(
          frame.workflow_copilot_chat_id,
          frame.turn_id,
        )
      )
        return;
      const reservation = copilotReservation.current;
      if (!reservation || !isCopilotTurnCurrent(reservation)) return;
      credentialResponseInFlight.current = true;
      const generation = recoveryGeneration.current;
      const chatId = workflowCopilotChatIdRef.current;
      try {
        // Copilot routes live on base_router (no /api/v1 prefix), like cancel.
        const client = await getClient(credentialGetter, "sans-api-v1");
        if (!isCopilotTurnCurrent(reservation)) return;
        await client.post("/workflow/copilot/credential-response", {
          turn_id: frame.turn_id,
          workflow_copilot_chat_id: frame.workflow_copilot_chat_id,
          resume_token: frame.resume_token,
          action,
          credential_id: action === "connected" ? credentialId : undefined,
        });
        if (
          !isCopilotTurnCurrent(reservation) ||
          recoveryGeneration.current !== generation ||
          workflowCopilotChatIdRef.current !== chatId ||
          // A new chat's first turn has no chat id client-side until the turn ends.
          (chatId !== null && chatId !== frame.workflow_copilot_chat_id)
        ) {
          return;
        }
        if (!streamingAbortController.current) {
          recoverCredentialTurn.current(
            frame.workflow_copilot_chat_id,
            frame.turn_id,
          );
        }
        const resolution: CredentialResolution =
          action === "connected"
            ? { outcome: "connected", credentialId, name }
            : { outcome: "skipped" };
        // The waiter's credential_pause_resolved frame can land first and carries the admitted verdict.
        setPauseCardResolutions((prev) =>
          prev[frame.resume_token]
            ? prev
            : withCappedResolution(prev, frame.resume_token, resolution),
        );
        // An update card fixes the credential the turn already chose, so the turn's answer stays put.
        if (!UPDATE_ASK_REASONS.includes(frame.reason)) {
          setCredentialResolutions((prev) =>
            withCappedResolution(prev, frame.turn_id, resolution),
          );
        }
      } catch (error) {
        if (!isCopilotTurnCurrent(reservation)) return;
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
    [credentialGetter, isCopilotTurnCurrent],
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
      const couldContinue = canContinue && Boolean(name);
      // ONLY the Accept fence. A turn in flight also blocks the continuation, but that case
      // records its receipt deliberately and is covered by its own test; widening this to cover
      // it would change behaviour nobody asked to change.
      const blockedForNow = couldContinue && acceptUnresolved;
      const shouldContinue =
        couldContinue && !isLoading && !isLoadingHistory && !acceptUnresolved;
      if (blockedForNow) {
        // Recording a resolution here would swap the picker for a "Credential added" ✓ on a turn
        // that never continued, and nothing re-attempts once the block lifts. Leaving it
        // unrecorded keeps the card in ask mode so the user can choose again. Note the distinction
        // from `!couldContinue`: there the receipt is correct, because there is nothing to
        // continue and the credential really was added.
        return;
      }
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
    [acceptUnresolved, isLoading, isLoadingHistory, resolveTerminalCredential],
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
      editingCredential?: CredentialApiResponse,
    ) => {
      pendingCredentialConnect.current = {
        frame,
        turnId,
        isLastMessage,
        editingCredential,
      };
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
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const { getSaveData } = useWorkflowHasChangesStore();
  const saveDataGetter = useRef(getSaveData);
  saveDataGetter.current = getSaveData;
  const workflowMutationLocked = useWorkflowYamlEditorStore(
    (state) =>
      state.commitInProgress ||
      state.copilotAcceptance !== null ||
      state.authoringInProgress,
  );
  // Mirrors beginCopilotAcceptance: a capturing recording does not hold back queued messages.
  const queueDrainLocked = useWorkflowYamlEditorStore(
    (state) =>
      state.commitInProgress ||
      state.copilotAcceptance !== null ||
      (state.authoringInProgress &&
        (!recordingAuthoringActive || recordingIsFinishing)),
  );
  const hasInitializedPosition = useRef(false);
  const hasAutoSentRef = useRef(false);
  const isWaitingForLiveBrowser = shouldWaitForLiveBrowser({
    requiresLiveBrowser,
    isLiveBrowserReady,
  });
  // Read, not taken: the receipt names the action count, and the packet itself is
  // taken (and cleared) only when the turn is actually posted.
  const armedRecordingEvidence = useRecordingRefinementEvidenceStore(
    (state) => state.armed,
  );
  // Reset on initialMessage/action change so a re-arrival of the prop (without a
  // remount) can fire auto-send again.
  useEffect(() => {
    hasAutoSentRef.current = false;
  }, [initialMessage, initialAction?.nonce]);
  // The server rewrites a typed action's message to its own receipt; echoing that same
  // text keeps the row from changing wording when the persisted history reloads.
  const autoSendMessage = !initialAction
    ? initialMessage
    : initialAction.kind === "diagnose_run"
      ? diagnoseRunReceipt(initialAction.workflowRunId)
      : refineRecordingReceipt(
          armedRecordingEvidence?.evidence.actions.length ?? 0,
        );
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
  const recordingChapterRef = useRef<HTMLDivElement | null>(null);
  const recordingFocusPortalRef = useRef<HTMLDivElement | null>(null);
  const [recordingInlinePortalEl, setRecordingInlinePortalEl] =
    useState<HTMLDivElement | null>(null);
  const [recordingSuggestionPortalEl, setRecordingSuggestionPortalEl] =
    useState<HTMLDivElement | null>(null);
  const recordingWasActiveRef = useRef(false);
  const [recordingBoundary, setRecordingBoundary] = useState<number | null>(
    null,
  );
  const [recordingFocusOpen, setRecordingFocusOpen] = useState(false);
  const [recordingCollapsed, setRecordingCollapsed] = useState(false);
  const [recordingChapterAboveViewport, setRecordingChapterAboveViewport] =
    useState(false);
  const [recordingProxyPreviewOpen, setRecordingProxyPreviewOpen] =
    useState(false);
  const [recordingProxyBaseline, setRecordingProxyBaseline] = useState(0);
  const recordingDraftSteps = useRecordingStore((state) => state.draftSteps);
  const recordingDeletedStepIds = useRecordingStore(
    (state) => state.deletedStepIds,
  );
  const dismissedCredentialStepIds = useRecordingStore(
    (state) => state.dismissedCredentialStepIds,
  );
  const recordingOptimisticSteps = useRecordingStore(
    (state) => state.optimisticSteps,
  );
  const recordingActionCount =
    recordingDraftSteps.filter(
      (step) => !recordingDeletedStepIds.includes(step.step_id),
    ).length + recordingOptimisticSteps.length;
  const visibleRecordingDraftSteps = recordingDraftSteps.filter(
    (step) => !recordingDeletedStepIds.includes(step.step_id),
  );
  const recordingSuggestionSignature = visibleRecordingDraftSteps
    .filter(
      (step) =>
        step.credential_kind &&
        !dismissedCredentialStepIds.includes(step.step_id),
    )
    .map((step) => `${step.step_id}:${step.credential_kind}`)
    .join(",");
  const outerFollowSignature = `${followSignature}|recording-suggestions:${recordingSuggestionSignature}`;
  const { scrollRef, isPinned, jumpToLatest, repin } =
    useStickToBottom<HTMLDivElement>(outerFollowSignature, { enabled: isOpen });
  const lastRecordingActionTitle =
    recordingOptimisticSteps[recordingOptimisticSteps.length - 1]?.title ??
    visibleRecordingDraftSteps[visibleRecordingDraftSteps.length - 1]?.title ??
    null;

  useEffect(() => {
    const wasActive = recordingWasActiveRef.current;
    if (recordingAuthoringActive && !wasActive) {
      setRecordingBoundary(messages.length);
      setRecordingFocusOpen(false);
      setRecordingCollapsed(false);
      setRecordingChapterAboveViewport(false);
      setRecordingProxyPreviewOpen(false);
      setRecordingProxyBaseline(recordingActionCount);
    } else if (!recordingAuthoringActive && wasActive) {
      setRecordingBoundary(null);
      setRecordingFocusOpen(false);
      setRecordingChapterAboveViewport(false);
      setRecordingProxyPreviewOpen(false);
    }
    recordingWasActiveRef.current = recordingAuthoringActive;
  }, [messages.length, recordingActionCount, recordingAuthoringActive]);

  useEffect(() => {
    const chapter = recordingChapterRef.current;
    const transcript = scrollRef.current;
    if (
      !recordingAuthoringActive ||
      recordingFocusOpen ||
      !chapter ||
      !transcript ||
      typeof IntersectionObserver === "undefined"
    ) {
      setRecordingChapterAboveViewport(false);
      return;
    }

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (!entry) return;
        const above =
          !entry.isIntersecting &&
          entry.rootBounds !== null &&
          entry.boundingClientRect.bottom <= entry.rootBounds.top;
        setRecordingChapterAboveViewport((wasAbove) => {
          if (above && !wasAbove) {
            setRecordingProxyBaseline(recordingActionCount);
          }
          return above;
        });
      },
      { root: transcript, threshold: 0.05 },
    );
    // The chapter is portaled into its inline host, so a moved host means a new
    // chapter element; re-observe or the stale node reads as scrolled away.
    observer.observe(chapter);
    return () => observer.disconnect();
  }, [
    recordingActionCount,
    recordingAuthoringActive,
    recordingFocusOpen,
    recordingInlinePortalEl,
    scrollRef,
  ]);

  const jumpToRecordingChapter = useCallback(() => {
    setRecordingProxyBaseline(recordingActionCount);
    setRecordingProxyPreviewOpen(false);
    recordingChapterRef.current?.scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  }, [recordingActionCount]);

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

  // A queued message's files already left the tray, so throwing the queue away would strand them.
  const discardQueuedPrompt = useCallback(() => {
    returnFilesToTray(queuedPromptRef.current?.attachments ?? []);
    updateQueuedPrompt(null);
  }, [returnFilesToTray, updateQueuedPrompt]);

  const handleNewChat = () => {
    if (acceptUnresolved) return;
    chatNavEpochRef.current += 1;
    setGateFailure(null);
    streamingAbortController.current?.abort();
    streamingAbortController.current = null;
    inFlightRef.current = false;
    setIsLoading(false);
    setRecoveredPauseFrames([]);
    setQuestionInteractions([]);
    setQuestionCancelToken(null);
    resetRecoveryPresentation();
    setMessages([]);
    discardQueuedPrompt();
    setWorkflowCopilotChatId(null);
    workflowCopilotChatIdRef.current = null;
    setProposedWorkflow(null);
    setPendingProposalMetadata(null);
    setPendingProposalRun(null);
    setPendingProposalTurnId(null);
    setAutoAccept(false);
    setWorkPlan([]);
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
      recoveredRecordingRefinement?: {
        responseIndex: number;
        turnId: string;
        messageId: string;
        status: RecordingRefinementStatus;
      },
      // Pass for a re-read of the chat on screen; a chat switch or first load always takes the row's value.
      autoAcceptWritesAtRead?: number,
    ) => {
      restoreAcceptFromHistory.current(data);
      rememberRecoveryCancelTokens(data);
      setRecoveredPauseFrames(data.pending_credential_requests ?? []);
      setQuestionInteractions(data.question_interactions ?? []);
      setQuestionCancelToken(data.pending_question_cancel_token ?? null);
      const historyMessages: ChatMessage[] = data.chat_history.map(
        (message, index) => ({
          id: `${index}-${Date.now()}`,
          sender: message.sender,
          content: message.content,
          timestamp: message.created_at,
          messageId: message.workflow_copilot_chat_message_id ?? undefined,
          feedback: message.feedback ?? null,
          attachedFiles:
            message.attached_files && message.attached_files.length > 0
              ? message.attached_files
              : undefined,
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
        }),
      );
      for (let index = 0; index < data.chat_history.length; index += 1) {
        const productRow = data.chat_history[index];
        const productMessage = historyMessages[index];
        if (!productRow || !productMessage) continue;
        const actionCount = refineRecordingActionCount(productRow.content);
        const turnId = productRow.turn_id ?? undefined;
        if (
          productRow.sender !== "product" ||
          actionCount === null ||
          turnId === undefined
        ) {
          continue;
        }

        const responseIndex = data.chat_history.findIndex(
          (message) =>
            message.sender === "ai" &&
            message.turn_outcome?.copilot_turn_id === turnId,
        );
        const responseRow =
          responseIndex >= 0 ? data.chat_history[responseIndex] : undefined;
        const responseMessage =
          responseIndex >= 0 ? historyMessages[responseIndex] : undefined;
        const terminalReason = responseRow?.turn_outcome?.terminal_reason;
        const ownsChatProposal = Boolean(
          turnId &&
          data.proposed_workflow &&
          data.proposed_workflow_metadata?.owner_turn_id === turnId,
        );
        const producedWorkflow = Boolean(
          responseMessage?.narrative?.proposalDisposition !== "no_proposal" &&
          (responseMessage?.narrative?.draft || ownsChatProposal),
        );
        const status: RecordingRefinementStatus = !responseRow
          ? "working"
          : isCancelledRefinementTurn(
                terminalReason,
                responseMessage?.narrative,
              )
            ? "cancelled"
            : terminalReason === INTERRUPTED_TERMINAL_REASON ||
                responseMessage?.narrative?.terminal === "error" ||
                (responseMessage?.narrative &&
                  notConfirmedOutcome(responseMessage.narrative) !== null) ||
                !producedWorkflow
              ? "failed"
              : "complete";
        historyMessages[index] = {
          ...productMessage,
          id: `recording-refinement-${turnId}`,
          kind: "recording_refinement",
          recordingRefinement: {
            actionCount,
            startedAtMs: parseServerStamp(productRow.created_at) || Date.now(),
            status,
            turnId,
          },
        };
      }
      // A rehydrated turn still owns the run its records name, so the
      // lifecycle hook keeps announcing nothing about it after a reload.
      for (const message of historyMessages) {
        const rehydratedRunId = message.narrative?.turnFacts?.runId;
        if (rehydratedRunId) rememberTurnOwnedRun(rehydratedRunId);
      }
      const restoredPendingProposalTurnId = data.proposed_workflow
        ? (data.proposed_workflow_metadata?.owner_turn_id ??
          getLatestDiffCardTurnId(historyMessages))
        : null;
      latestTurnId.current = restoredPendingProposalTurnId;
      // History never carries run_lifecycle lines (local-only); carry them
      // forward only for the mount-race caller, not an explicit chat switch.
      setMessages((prev) => {
        const initialHandoff = carryForwardLifecycle
          ? prev.find((message) => message.kind === "initial_handoff")
          : undefined;
        const historyIncludesInitialHandoff =
          initialHandoff !== undefined &&
          historyMessages.some(
            (message) =>
              message.sender === "user" &&
              message.content === initialHandoff.content,
          );
        const nextMessages: ChatMessage[] = [
          ...(initialHandoff && !historyIncludesInitialHandoff
            ? [initialHandoff]
            : []),
          ...historyMessages,
          ...(carryForwardLifecycle
            ? prev.filter((message) => message.kind === "run_lifecycle")
            : []),
        ];
        const recovery = recoveredRecordingRefinement;
        const hydratedProgressIndex = recovery
          ? nextMessages.findIndex(
              (message) =>
                message.kind === "recording_refinement" &&
                message.recordingRefinement?.turnId === recovery.turnId,
            )
          : -1;
        const progressMessage = recovery
          ? (prev.find(
              (message) =>
                message.id === recovery.messageId &&
                message.kind === "recording_refinement" &&
                message.recordingRefinement?.turnId === recovery.turnId,
            ) ??
            (hydratedProgressIndex >= 0
              ? nextMessages[hydratedProgressIndex]
              : undefined))
          : undefined;
        if (!recovery || !progressMessage?.recordingRefinement) {
          return nextMessages;
        }
        const completedProgress: ChatMessage = {
          ...progressMessage,
          recordingRefinement: {
            ...progressMessage.recordingRefinement,
            status: recovery.status,
          },
        };
        if (hydratedProgressIndex >= 0) {
          nextMessages[hydratedProgressIndex] = completedProgress;
        } else {
          nextMessages.splice(
            Math.max(0, recovery.responseIndex),
            0,
            completedProgress,
          );
        }
        return nextMessages;
      });
      // Bumped here rather than when navigation starts, so a history load that FAILS does not
      // strand an in-flight write: the user is still in the old chat, and a completion clearing
      // that chat's state is correct. Synchronous and in the same block as the state write, so
      // it lands a commit before the effect that syncs workflowCopilotChatIdRef. Compared against
      // what hydration last applied, never against that lagging ref, and only once a chat has
      // been applied - a first hydration is not navigation, and New chat bumps for itself.
      const previouslyHydratedChatId = hydratedChatIdRef.current;
      hydratedChatIdRef.current = data.workflow_copilot_chat_id;
      if (
        previouslyHydratedChatId !== null &&
        data.workflow_copilot_chat_id !== previouslyHydratedChatId
      ) {
        chatNavEpochRef.current += 1;
      }
      if (
        workflowPermanentId &&
        !useWorkflowYamlEditorStore.getState().pendingAccepts[
          workflowPermanentId
        ]
      )
        useWorkflowTitleStore
          .getState()
          .trackCopilotMetadata(
            workflowPermanentId,
            data.proposed_workflow
              ? `${data.workflow_copilot_chat_id}:${proposalTokenOf(data.proposed_workflow_metadata ?? null) ?? "legacy"}`
              : null,
          );
      setWorkflowCopilotChatId(data.workflow_copilot_chat_id);
      if (
        data.proposed_workflow ||
        !pendingCanonicalRecovery.current ||
        pendingCanonicalRecovery.current.chatId !==
          data.workflow_copilot_chat_id
      ) {
        setProposedWorkflow(data.proposed_workflow ?? null);
        setPendingProposalMetadata(data.proposed_workflow_metadata ?? null);
        setPendingProposalRun(data.proposed_workflow_run ?? null);
        setPendingProposalTurnId(
          data.proposed_workflow ? restoredPendingProposalTurnId : null,
        );
      }
      if (
        autoAcceptWritesAtRead === undefined ||
        autoAcceptWritesAtRead === autoAcceptWrites.current
      ) {
        setAutoAccept(data.auto_accept ?? false);
      }
      setWorkPlan(data.work_plan ?? []);
    },
    [rememberTurnOwnedRun, rememberRecoveryCancelTokens, workflowPermanentId],
  );

  const stopRecoveryPolls = useCallback(() => {
    // Bumping the generation also discards responses already in flight, which
    // clearTimeout alone cannot cancel.
    recoveryGeneration.current += 1;
    chatPresentationGeneration.current += 1;
    canonicalRecoveryAbort.current?.abort();
    recoveryPolls.current.forEach((poll) => poll.stop());
    recoveryPolls.current.clear();
    if (
      copilotReservation.current &&
      !Object.values(useWorkflowYamlEditorStore.getState().pendingAccepts).some(
        (item) => item.reservation === copilotReservation.current,
      )
    )
      finishCopilotAcceptance(copilotReservation.current);
    copilotReservation.current = null;
    turnOwner.current = null;
    pendingCanonicalRecovery.current = null;
    setRecoveryControls(null);
    recoverySnapshotStamps.current.clear();
    recoveryCancelTokens.current.clear();
    recoveredTurnOwnerRef.current = null;
  }, []);

  useEffect(() => {
    const pending = pendingCanonicalRecovery.current;
    if (
      outstandingAccept ||
      pending?.workflowPermanentId !== workflowPermanentId ||
      !pending?.definitiveRejection
    )
      return;
    const description = pending.definitiveRejection;
    stopRecoveryPolls();
    setGateFailure(null);
    setIsAccepting(false);
    toast({ title: "Accept failed", description, variant: "destructive" });
  }, [outstandingAccept, stopRecoveryPolls, workflowPermanentId]);

  const resetRecoveryPresentation = useCallback(() => {
    chatPresentationGeneration.current += 1;
    for (const poll of new Set(recoveryPolls.current.values())) {
      if (!poll.isReserved?.()) poll.stop();
    }
    recoveredTurnOwnerRef.current = null;
  }, []);

  const createCanonicalRecovery = useCallback(
    (
      reservation: symbol,
      options: Partial<CanonicalRecovery> = {},
    ): CanonicalRecovery | null => {
      if (!workflowPermanentId || !isCopilotTurnCurrent(reservation))
        return null;
      const yaml = useWorkflowYamlEditorStore.getState();
      const saveData = saveDataGetter.current();
      const pending: CanonicalRecovery = {
        workflowPermanentId,
        chatId: workflowCopilotChatIdRef.current,
        baseline: saveData?.workflow,
        preservedSettings: structuredClone(saveData?.settings),
        waitingForUnlock: false,
        yaml: yaml.active
          ? { draft: yaml.draft, entrySnapshot: yaml.entrySnapshot }
          : null,
        ...options,
      };
      pendingCanonicalRecovery.current = pending;
      return pending;
    },
    [isCopilotTurnCurrent, workflowPermanentId],
  );

  const startRecoveryPoll = useCallback(
    (
      chatId: string | null,
      turnId: string | null,
      requestId = turnId ?? crypto.randomUUID(),
      reservation?: symbol,
      ownsTurn = false,
      recordingRefinementMessageId?: string,
      presentationGeneration = chatPresentationGeneration.current,
    ) => {
      const editor = useWorkflowYamlEditorStore.getState().editorOwner;
      if (
        !workflowPermanentId ||
        !composerMountedRef.current ||
        (editor &&
          (!editor.active ||
            editor.workflowPermanentId !== workflowPermanentId)) ||
        (reservation && !isCopilotTurnCurrent(reservation))
      )
        return;
      const existing =
        recoveryPolls.current.get(requestId) ??
        (turnId ? recoveryPolls.current.get(turnId) : undefined) ??
        (ownsTurn
          ? [...new Set(recoveryPolls.current.values())].find((poll) =>
              poll.canAdopt?.(chatId),
            )
          : undefined);
      if (
        existing?.adopt?.(chatId, turnId, {
          requestId,
          reservation,
          ownsTurn,
          recordingRefinementMessageId,
        })
      ) {
        return existing.adopt;
      }
      const generation = recoveryGeneration.current;
      const isPresented = () =>
        chatPresentationGeneration.current === presentationGeneration ||
        (chatId !== null && workflowCopilotChatIdRef.current === chatId);
      let canonicalRecovery = reservation
        ? pendingCanonicalRecovery.current
        : null;
      let holdingReservation = reservation !== undefined;
      const isCurrent = () =>
        composerMountedRef.current &&
        recoveryGeneration.current === generation &&
        useWorkflowYamlEditorStore.getState().editorOwner === editor &&
        (!editor || editor.active) &&
        (!holdingReservation ||
          (reservation !== undefined && isCopilotTurnCurrent(reservation)));
      const claimRecovery = (terminalConfirmed = false) => {
        if (!isCurrent()) return false;
        reservation ??= reserveCopilotTurn() ?? undefined;
        if (!reservation) return false;
        holdingReservation = true;
        canonicalRecovery ??= createCanonicalRecovery(reservation, {
          awaitingTurnId: turnId ?? undefined,
          terminalConfirmed,
          hadLocalEdits:
            useWorkflowHasChangesStore.getState().hasChanges ||
            (useWorkflowYamlEditorStore.getState().active &&
              (useWorkflowYamlEditorStore.getState().stale ||
                useWorkflowYamlEditorStore.getState().draft !==
                  useWorkflowYamlEditorStore.getState().entrySnapshot)),
        });
        return canonicalRecovery !== null;
      };
      if ((ownsTurn || holdingReservation) && !claimRecovery()) return;
      const deadline =
        canonicalRecovery?.poll?.deadline ??
        Date.now() + RECOVERY_POLL_BUDGET_MS;
      if (canonicalRecovery)
        canonicalRecovery.poll = {
          chatId,
          turnId,
          requestId,
          deadline,
          ownsTurn,
          recordingRefinementMessageId,
        };
      let timer: ReturnType<typeof setTimeout> | null = null;
      let step = 0;
      let appliedContent: string | null = null;
      let schedulesRefreshed = false;
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
      let successor: RecoveryPoll | null = null;
      let retryRequested = false;
      let rejecting = false;
      let deadlineReached = false;

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
      // A failed stream cannot prove the server did not commit. Keep the
      // editor reserved until reconciliation succeeds or its owner is disposed.
      function releaseReservation() {
        if (!holdingReservation || !reservation) return;
        holdingReservation = false;
        if (
          !isCurrent() &&
          Object.values(
            useWorkflowYamlEditorStore.getState().pendingAccepts,
          ).some((item) => item.reservation === reservation)
        )
          return;
        if (pendingCanonicalRecovery.current === canonicalRecovery)
          pendingCanonicalRecovery.current = null;
        finishCopilotAcceptance(reservation);
        if (copilotReservation.current === reservation)
          copilotReservation.current = null;
      }
      function finish() {
        if (stopped) return;
        const current = isCurrent();
        stopped = true;
        releaseReservation();
        clearTimer();
        if (deadlineTimer !== null) {
          clearTimeout(deadlineTimer);
          deadlineTimer = null;
        }
        for (const [key, poll] of recoveryPolls.current) {
          if (poll === operation) recoveryPolls.current.delete(key);
        }
        if (
          canonicalRecovery &&
          !successor &&
          pendingCanonicalRecovery.current === null
        )
          setRecoveryControls(null);
        if (!current) return;
        if (turnId !== null && recoveredTurnOwnerRef.current === turnId) {
          recoveredTurnOwnerRef.current = null;
          setIsLoading(false);
        }
      }

      // Ending without the turn's row leaves a notice describing work that has
      // stopped, so it reverts to the plain failure it replaced.
      function giveUp() {
        if (!isCurrent()) {
          finish();
          return;
        }
        finish();
        setMessages((prev) =>
          prev.map((message) =>
            message.recordingRefinement?.turnId === turnId &&
            message.recordingRefinement.status === "working"
              ? {
                  ...message,
                  recordingRefinement: {
                    ...message.recordingRefinement,
                    status: "failed",
                  },
                }
              : (message.recoveryTurnId === turnId ||
                    message.recoveryTurnId === requestId) &&
                  message.content === RECOVERY_IN_PROGRESS_MESSAGE
                ? { ...message, content: SEND_FAILED_MESSAGE }
                : message,
          ),
        );
      }

      const retry = () => {
        if (stopped || !isCurrent()) return;
        clearTimer();
        if (canonicalRecovery?.retryAcceptAvailable)
          canonicalRecovery.retryAccept = true;
        if (canonicalRecovery?.savedWorkflow) {
          void tick(true);
          return;
        }
        if (deadlineReached) {
          timer = setTimeout(() => {
            timer = null;
            void tick(true);
          }, 0);
          return;
        }
        retryRequested = true;
        schedule();
      };
      function installRecoveryControls() {
        if (!isCurrent() || !holdingReservation || !canonicalRecovery) return;
        if (deadlineReached) {
          setMessages((messages) =>
            messages.filter(
              (message) =>
                message.content !== RECOVERY_IN_PROGRESS_MESSAGE ||
                (message.recoveryTurnId !== turnId &&
                  message.recoveryTurnId !== requestId),
            ),
          );
        }
        if (
          canonicalRecovery.acceptChatId &&
          canonicalRecovery.gateAttempt &&
          !canonicalRecovery.unattributedClaim
        ) {
          const recovery = canonicalRecovery;
          const gateAttempt = canonicalRecovery.gateAttempt;
          setGateFailure((current) =>
            current?.kind === "changed" && gateAttempt?.wroteNothing
              ? current
              : recovery.savedWorkflow
                ? {
                    kind: "saved",
                    savedWorkflow: recovery.savedWorkflow,
                    ownerTurnId: recovery.savedOwnerTurnId ?? null,
                    ...gateAttempt,
                  }
                : deadlineReached
                  ? { kind: "reload", attempt: gateAttempt }
                  : {
                      kind: "recover",
                      ...gateAttempt,
                    },
          );
        }
        setRecoveryControls({
          retry,
          cancelling: canonicalRecovery.cancellationRequested,
          ...(deadlineReached ? { reload: retry } : {}),
          ...(canonicalRecovery.localDraftConflict
            ? {
                conflict: {
                  keep: () => {
                    if (!isCurrent() || !canonicalRecovery) return;
                    canonicalRecovery.draftChoice = "keep";
                    retry();
                  },
                  discard: () => {
                    if (!isCurrent() || !canonicalRecovery) return;
                    canonicalRecovery.draftChoice = "discard";
                    retry();
                  },
                },
              }
            : {}),
          reject: () => {
            if (stopped || !isCurrent() || rejecting) return;
            if (
              canonicalRecovery?.acceptChatId ||
              (!canonicalRecovery?.awaitingTurnId &&
                canonicalRecovery?.terminalConfirmed)
            ) {
              retry();
              return;
            }
            const cancelToken =
              (turnId ? recoveryCancelTokens.current.get(turnId) : undefined) ??
              (requestId !== turnId ? requestId : undefined);
            if (!cancelToken) {
              toast({
                title: "Could not cancel the Copilot turn",
                description:
                  "The saved turn has no cancellation token. Retry to check its status.",
                variant: "destructive",
              });
              retry();
              return;
            }
            rejecting = true;
            canonicalRecovery!.cancellationRequested = true;
            canonicalRecovery!.restoreRollback = true;
            installRecoveryControls();
            void (async () => {
              try {
                const client = await getClient(credentialGetter, "sans-api-v1");
                if (stopped || !isCurrent()) return;
                await client.post(
                  "/workflow/copilot/cancel",
                  {
                    cancel_token: cancelToken,
                    workflow_copilot_chat_id: chatId,
                    source: "stop_button",
                  },
                  { timeout: CANONICAL_READ_TIMEOUT_MS },
                );
              } catch (error) {
                if (stopped || !isCurrent()) return;
                console.error("Failed to cancel the recovering Copilot turn:", {
                  status: getErrorStatus(error),
                  requestId,
                });
                toast({
                  title: "Could not cancel the Copilot turn",
                  description: "Copilot will keep checking for saved changes.",
                  variant: "destructive",
                });
              } finally {
                rejecting = false;
                retry();
              }
            })();
          },
        });
      }
      // A read that never returns leaves schedule() unreached, so the budget
      // needs a timer of its own or the deadline can never fire.
      function checkDeadline() {
        if (stopped || deadlineReached) return;
        if (!isCurrent()) {
          finish();
          return;
        }
        if (!holdingReservation) {
          giveUp();
          return;
        }
        deadlineReached = true;
        clearTimer();
        void tick(true);
      }
      if (!canonicalRecovery?.savedWorkflow)
        deadlineTimer = setTimeout(
          checkDeadline,
          Math.max(0, deadline - Date.now()),
        );

      function schedule() {
        if (deadlineReached) return;
        clearTimer();
        // finish() aborts the in-flight read, which surfaces in tick's catch;
        // without this the rejection would reschedule a stopped poll.
        if (stopped) {
          return;
        }
        if (Date.now() >= deadline) {
          checkDeadline();
          return;
        }
        const delay =
          retryRequested || (canonicalRecovery?.acceptChatId && step === 0)
            ? 0
            : (RECOVERY_POLL_DELAYS_MS[step] ?? RECOVERY_POLL_STEADY_MS);
        retryRequested = false;
        step += 1;
        timer = setTimeout(() => {
          timer = null;
          void tick();
        }, delay);
      }

      async function tick(finalCheck = false) {
        if (!isCurrent()) {
          finish();
          return;
        }
        const sendEpochBeforeRead = sendEpoch.current;
        const autoAcceptWritesBeforeRead = autoAcceptWrites.current;
        const controller = new AbortController();
        inFlight = controller;
        let canonicalReadAttempted = false;
        let historyTimeout: ReturnType<typeof setTimeout> | undefined;
        try {
          if (
            holdingReservation &&
            (!canonicalRecovery?.awaitingTurnId ||
              canonicalRecovery.localDraftConflict) &&
            canonicalRecovery?.terminalConfirmed !== false
          ) {
            canonicalReadAttempted = true;
            await reconcileCanonicalWorkflowRef.current?.(
              reservation,
              controller.signal,
            );
            if (stopped || !isCurrent()) {
              finish();
              return;
            }
            if (inFlight !== controller || controller.signal.aborted) return;
            if (pendingCanonicalRecovery.current !== canonicalRecovery) {
              if (turnId === null) {
                setMessages((prev) =>
                  prev.filter(
                    (message) =>
                      message.recoveryTurnId !== (turnId ?? requestId) ||
                      message.content !== RECOVERY_IN_PROGRESS_MESSAGE,
                  ),
                );
              }
              finish();
              return;
            }
          }
          if (
            (canonicalRecovery?.acceptChatId &&
              canonicalRecovery.terminalConfirmed !== false) ||
            canonicalRecovery?.localDraftConflict
          ) {
            installRecoveryControls();
            schedule();
            return;
          }
          const historyRead = (async () => {
            const client = await getClient(credentialGetter, "sans-api-v1");
            if (stopped || controller.signal.aborted)
              throw new Error("History read cancelled");
            return readCredentialRecoveryHistory<WorkflowCopilotChatHistoryResponse>(
              client,
              workflowPermanentId,
              {
                params: {
                  ...(chatId
                    ? { workflow_copilot_chat_id: chatId }
                    : { workflow_permanent_id: workflowPermanentId }),
                  ...(holdingReservation && turnId === null
                    ? { request_cancel_token: requestId }
                    : {}),
                },
                signal: controller.signal,
              },
            );
          })();
          const response = finalCheck
            ? await Promise.race([
                historyRead,
                new Promise<never>((_, reject) => {
                  const abort = () =>
                    reject(
                      new Error("Final history read cancelled or timed out"),
                    );
                  controller.signal.addEventListener("abort", abort, {
                    once: true,
                  });
                  historyTimeout = setTimeout(
                    () => controller.abort(),
                    CANONICAL_READ_TIMEOUT_MS,
                  );
                }),
              ])
            : await historyRead;
          clearTimeout(historyTimeout);
          if (stopped || inFlight !== controller || controller.signal.aborted)
            return;
          // Without a chat id the read resolves "the workflow's latest chat",
          // which another tab can move by creating a newer one. Latching the
          // first id keeps every later read on the chat this turn recovered in.
          if (chatId === null && response.data.workflow_copilot_chat_id) {
            chatId = response.data.workflow_copilot_chat_id;
          }
          if (stopped || !isCurrent()) {
            finish();
            return;
          }
          if (turnId === null && response.data.request_turn_id) {
            adopt(chatId, response.data.request_turn_id);
          }
          rememberRecoveryCancelTokens(response.data);
          const pendingCredentialRequests =
            response.data.pending_credential_requests ?? [];
          if (isPresented()) {
            setRecoveredPauseFrames(pendingCredentialRequests);
            setQuestionInteractions(response.data.question_interactions ?? []);
            setQuestionCancelToken(
              response.data.pending_question_cancel_token ?? null,
            );
          }
          // The backend can persist the pause before its SSE frame reaches the
          // browser. Once history proves this poll owns a credential wait, it
          // must block another turn just like a poll armed from the live frame.
          if (
            !ownsTurn &&
            turnId !== null &&
            (pendingCredentialRequests.some(
              (item) => item.turn_id === turnId,
            ) ||
              response.data.question_interactions?.some(
                (item) => item.turn_id === turnId && item.status === "pending",
              ))
          ) {
            if (!claimRecovery()) {
              schedule();
              return;
            }
            ownsTurn = true;
            installRecoveryControls();
            if (isPresented()) {
              recoveredTurnOwnerRef.current = turnId;
              setIsLoading(true);
            }
          }
          const pendingQuestion = response.data.question_interactions?.find(
            (item) => item.turn_id === turnId && item.status === "pending",
          );
          if (pendingQuestion) {
            if (isPresented()) {
              setWorkflowCopilotChatId(chatId);
              workflowCopilotChatIdRef.current = chatId;
              setQuestionInteractions(
                response.data.question_interactions ?? [],
              );
              setQuestionCancelToken(
                response.data.pending_question_cancel_token ?? null,
              );
              if (
                holdingReservation ||
                (turnId !== null &&
                  (inFlightRef.current
                    ? narrativeRef.current.turnId === turnId
                    : recoveredTurnOwnerRef.current === turnId))
              ) {
                setIsLoading(false);
              }
            }
            if (holdingReservation) {
              schedule();
            } else {
              finish();
            }
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
          const row =
            turnId === null
              ? null
              : findRecoveredRow(
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
            const recoveryChatId =
              response.data.workflow_copilot_chat_id ?? chatId;
            const snapshotStamp = response.data.chat_history.reduce(
              (newest, message) =>
                Math.max(
                  newest,
                  parseServerStamp(message.modified_at ?? message.created_at),
                ),
              Number.NEGATIVE_INFINITY,
            );
            if (recoveryChatId && isPresented()) {
              // Concurrent polls replace the full history. A slower response
              // captured before a newer terminal row must not erase that row.
              const latestAppliedStamp =
                recoverySnapshotStamps.current.get(recoveryChatId) ??
                Number.NEGATIVE_INFINITY;
              if (snapshotStamp < latestAppliedStamp) {
                schedule();
                return;
              }
              recoverySnapshotStamps.current.set(recoveryChatId, snapshotStamp);
            }
            if (isPresented() && row.content !== appliedContent) {
              appliedContent = row.content;
              const recoveredNarrative = hydrateHistoryNarrative(
                row.narrative_payload,
                row.turn_outcome,
              );
              const interrupted =
                row.turn_outcome?.terminal_reason ===
                INTERRUPTED_TERMINAL_REASON;
              const ownsChatProposal = Boolean(
                response.data.proposed_workflow &&
                response.data.proposed_workflow_metadata?.owner_turn_id ===
                  turnId,
              );
              const recoveredProducedWorkflow = Boolean(
                recoveredNarrative?.proposalDisposition !== "no_proposal" &&
                (recoveredNarrative?.draft || ownsChatProposal),
              );
              const recoveredStatus: RecordingRefinementStatus = interrupted
                ? holdingReservation
                  ? "failed"
                  : "working"
                : isCancelledRefinementTurn(
                      row.turn_outcome?.terminal_reason,
                      recoveredNarrative,
                    )
                  ? "cancelled"
                  : recoveredNarrative?.terminal === "error" ||
                      (recoveredNarrative &&
                        notConfirmedOutcome(recoveredNarrative) !== null) ||
                      !recoveredProducedWorkflow
                    ? "failed"
                    : "complete";
              applyHistoryResponse(
                response.data,
                true,
                recordingRefinementMessageId && turnId
                  ? {
                      responseIndex: response.data.chat_history.indexOf(row),
                      turnId,
                      messageId: recordingRefinementMessageId,
                      status: recoveredStatus,
                    }
                  : undefined,
                autoAcceptWritesBeforeRead,
              );
            }
            // An interrupted row can be replaced by the still-running finalizer.
            if (
              row.turn_outcome?.terminal_reason !== INTERRUPTED_TERMINAL_REASON
            ) {
              if (!schedulesRefreshed) {
                schedulesRefreshed = true;
                void queryClient.invalidateQueries({
                  queryKey: ["workflowSchedules", workflowPermanentId],
                });
              }
              if (holdingReservation) {
                const narrative = hydrateHistoryNarrative(
                  row.narrative_payload,
                  row.turn_outcome,
                );
                const rowTurnId = row.turn_outcome?.copilot_turn_id;
                const reason = row.turn_outcome?.terminal_reason;
                if (
                  canonicalRecovery?.awaitingTurnId &&
                  canonicalRecovery.awaitingTurnId === rowTurnId
                ) {
                  canonicalRecovery.terminalConfirmed = true;
                  canonicalRecovery.restoreRollback ||=
                    isCancelledRefinementTurn(reason, narrative) ||
                    narrative?.terminal === "error" ||
                    (!!reason &&
                      [
                        "cancelled",
                        "error",
                        "copilot_recoverable_failure",
                      ].includes(reason));
                  canonicalReadAttempted = true;
                  await reconcileCanonicalWorkflowRef.current?.(
                    reservation,
                    controller.signal,
                  );
                  if (
                    stopped ||
                    !isCurrent() ||
                    inFlight !== controller ||
                    controller.signal.aborted
                  )
                    return;
                  if (pendingCanonicalRecovery.current !== canonicalRecovery) {
                    finish();
                    return;
                  }
                }
                installRecoveryControls();
                schedule();
                return;
              }
              if (useWorkflowHasChangesStore.getState().hasChanges) {
                finish();
                return;
              }
              if (claimRecovery(true)) {
                installRecoveryControls();
                await reconcileCanonicalWorkflowRef.current?.(
                  reservation,
                  controller.signal,
                );
                if (stopped || !isCurrent() || controller.signal.aborted)
                  return;
                if (pendingCanonicalRecovery.current !== canonicalRecovery) {
                  finish();
                  return;
                }
              }
              schedule();
              return;
            }
            // A turn cancelled operationally (a worker drain) writes this row
            // and never finishes, so waiting out the budget would have every
            // open chat polling in lockstep through a deploy. The user already
            // has a truthful row; only a few reads are spent on a supersede.
            supersedeReadsLeft -= 1;
            if (supersedeReadsLeft <= 0 && !holdingReservation) {
              giveUp();
              return;
            }
          }
        } catch (error) {
          if (stopped || inFlight !== controller) return;
          console.warn("Copilot recovery poll failed:", error);
          if (!isCurrent()) {
            finish();
            return;
          }
          // The usual cause of a severed stream is the client losing the
          // network, and then every read fails too. Rescheduling through that
          // holds the notice for the whole budget and withholds the error the
          // user used to get at once, so a run of failures ends the poll.
          consecutiveFailures += 1;
          if (
            consecutiveFailures >= RECOVERY_POLL_FAILURE_CEILING &&
            !holdingReservation
          ) {
            giveUp();
            return;
          }
          schedule();
          return;
        } finally {
          clearTimeout(historyTimeout);
          if (
            finalCheck &&
            !stopped &&
            inFlight === controller &&
            isCurrent()
          ) {
            // Even an unavailable history endpoint cannot skip the final canonical read.
            if (holdingReservation && !canonicalReadAttempted) {
              await reconcileCanonicalWorkflowRef.current?.(
                reservation,
                controller.signal.aborted ? undefined : controller.signal,
              );
            }
            if (!stopped && isCurrent() && inFlight === controller) {
              if (pendingCanonicalRecovery.current !== canonicalRecovery)
                finish();
              else installRecoveryControls();
            }
          }
          if (inFlight === controller) {
            inFlight = null;
          }
        }
        consecutiveFailures = 0;
        schedule();
      }

      const adopt: NonNullable<RecoveryPoll["adopt"]> = (
        recoveredChatId,
        recoveredTurnId,
        options,
      ) => {
        if (successor)
          return (
            successor.adopt?.(recoveredChatId, recoveredTurnId, options) ??
            false
          );
        if (
          stopped ||
          !isCurrent() ||
          (options?.reservation && !isCopilotTurnCurrent(options.reservation))
        )
          return false;
        const collision = recoveredTurnId
          ? recoveryPolls.current.get(recoveredTurnId)
          : undefined;
        if (collision && collision !== operation) {
          const ownership = collision.retire?.(operation);
          if (ownership) {
            chatId ??= ownership.chatId;
            ownsTurn ||= ownership.ownsTurn;
            recordingRefinementMessageId ??=
              ownership.recordingRefinementMessageId;
            if (!holdingReservation && ownership.reservation) {
              reservation = ownership.reservation;
              holdingReservation = true;
              canonicalRecovery = ownership.canonicalRecovery;
              requestId = ownership.requestId;
            }
            for (const alias of ownership.aliases)
              recoveryPolls.current.set(alias, operation);
          }
        }
        chatId = recoveredChatId ?? chatId;
        const waitingForRequestCorrelation =
          holdingReservation &&
          turnId === null &&
          canonicalRecovery?.terminalConfirmed === false &&
          options?.ownsTurn &&
          !options.reservation;
        if (!waitingForRequestCorrelation) turnId = recoveredTurnId ?? turnId;
        if (options) {
          if (!holdingReservation && options.reservation) {
            reservation = options.reservation;
            holdingReservation = true;
            canonicalRecovery = pendingCanonicalRecovery.current;
            requestId = options.requestId;
          }
          if (options.ownsTurn && !claimRecovery()) return false;
          ownsTurn ||= options.ownsTurn;
          recordingRefinementMessageId ??= options.recordingRefinementMessageId;
          recoveryPolls.current.set(options.requestId, operation);
        }
        if ((ownsTurn || holdingReservation) && !claimRecovery()) return false;
        if (canonicalRecovery)
          canonicalRecovery.poll = {
            chatId,
            turnId,
            requestId,
            deadline,
            ownsTurn,
            recordingRefinementMessageId,
          };
        recoveryPolls.current.set(requestId, operation);
        if (turnId !== null) {
          recoveryPolls.current.set(turnId, operation);
          if (holdingReservation && canonicalRecovery) {
            if (!canonicalRecovery.terminalConfirmed)
              canonicalRecovery.awaitingTurnId = turnId;
            installRecoveryControls();
          }
          if (ownsTurn && isPresented()) {
            recoveredTurnOwnerRef.current = turnId;
            setIsLoading(true);
          }
        }
        return true;
      };
      const operation: RecoveryPoll = {
        stop: finish,
        retry,
        adopt,
        isReserved: () => holdingReservation,
        canAdopt: (recoveredChatId) =>
          isCurrent() &&
          holdingReservation &&
          turnId === null &&
          chatId !== null &&
          chatId === recoveredChatId,
        retire: (replacement) => {
          if (stopped || !isCurrent()) return;
          const ownership: RecoveryPollOwnership = {
            chatId,
            turnId,
            requestId,
            reservation: holdingReservation ? reservation : undefined,
            canonicalRecovery,
            ownsTurn,
            recordingRefinementMessageId,
            aliases: [...recoveryPolls.current].flatMap(([alias, poll]) =>
              poll === operation ? [alias] : [],
            ),
          };
          // Ownership moves before cleanup so retirement cannot unlock the editor.
          holdingReservation = false;
          successor = replacement;
          finish();
          return ownership;
        },
      };
      adopt(chatId, turnId);
      installRecoveryControls();
      if (!canonicalRecovery?.savedWorkflow) schedule();
      return adopt;
    },
    [
      applyHistoryResponse,
      credentialGetter,
      workflowPermanentId,
      rememberRecoveryCancelTokens,
      createCanonicalRecovery,
      isCopilotTurnCurrent,
      reserveCopilotTurn,
    ],
  );

  restoreAcceptFromHistory.current = (row) => {
    if (!workflowPermanentId || !row.workflow_copilot_chat_id) return;
    const remaining = row.proposed_claim_expires_in_seconds;
    const metadata = row.proposed_workflow_metadata;
    const stored = readStoredAccept(workflowPermanentId);
    if (
      !(typeof remaining === "number" && remaining > 0) &&
      metadata?.disposition !== "accepting" &&
      !stored
    )
      return;
    const parked =
      useWorkflowYamlEditorStore.getState().pendingAccepts[workflowPermanentId];
    if (parked) return;
    const reservation = copilotReservation.current ?? reserveCopilotTurn();
    if (!reservation || !isCopilotTurnCurrent(reservation)) {
      deferredStartupClaim.current = { workflowPermanentId, row };
      setStartupReadyFor(null);
      return;
    }
    const existing = pendingCanonicalRecovery.current;
    const snapshot: CopilotAcceptSnapshot & CanonicalRecovery = {
      ...existing,
      workflowPermanentId,
      chatId: row.workflow_copilot_chat_id,
      acceptChatId: row.workflow_copilot_chat_id,
      acceptAttempt: stored?.acceptAttempt ?? metadata ?? null,
      unattributedClaim: !metadata,
      gateAttempt: {
        alwaysAccept: stored?.alwaysAccept ?? row.auto_accept ?? false,
        token: proposalTokenOf(metadata ?? null),
        wroteNothing: false,
      },
      baseline: existing?.baseline ?? getSaveData()?.workflow,
      preservedSettings:
        existing?.preservedSettings ?? structuredClone(getSaveData()?.settings),
      hadLocalEdits:
        existing?.hadLocalEdits ??
        useWorkflowHasChangesStore.getState().hasChanges,
      terminalConfirmed: existing?.terminalConfirmed ?? true,
      waitingForUnlock: existing?.waitingForUnlock ?? false,
      yaml: existing?.yaml ?? null,
    };
    storePendingAccept(workflowPermanentId, {
      chatId: snapshot.chatId,
      acceptAttempt: snapshot.acceptAttempt,
      alwaysAccept: snapshot.gateAttempt?.alwaysAccept ?? false,
    });
    const recovery = existing
      ? (Object.assign(existing, snapshot) as typeof snapshot)
      : snapshot;
    pendingCanonicalRecovery.current = recovery;
    useWorkflowYamlEditorStore.setState((state) => ({
      pendingAccepts: {
        ...state.pendingAccepts,
        [workflowPermanentId]: { reservation, snapshot: recovery },
      },
    }));
    if (
      ![...recoveryPolls.current.values()].some((poll) => poll.isReserved?.())
    )
      startRecoveryPoll(snapshot.chatId, null, undefined, reservation);
  };

  const adoptRecoveredTurns = useCallback(
    (
      data: WorkflowCopilotChatHistoryResponse,
      hadCredentialRecoveryToken: boolean,
    ) => {
      const pendingRequests = data.pending_credential_requests ?? [];
      let pending: { turn_id: string } | undefined =
        pendingRequests[pendingRequests.length - 1] ??
        data.question_interactions?.find((item) => item.status === "pending");
      const completedTurnIds = new Set(
        data.chat_history.flatMap((message) =>
          message.sender === "ai" &&
          message.turn_outcome?.copilot_turn_id &&
          message.turn_outcome.terminal_reason !== INTERRUPTED_TERMINAL_REASON
            ? [message.turn_outcome.copilot_turn_id]
            : [],
        ),
      );
      if (
        !pending &&
        data.workflow_copilot_chat_id &&
        !hadCredentialRecoveryToken
      ) {
        const unanswered = [...data.chat_history]
          .reverse()
          .find(
            (message) =>
              message.sender === "user" &&
              message.turn_id &&
              !completedTurnIds.has(message.turn_id),
          );
        if (unanswered?.turn_id && !data.proposed_workflow) {
          pending = { turn_id: unanswered.turn_id };
          toast({
            title: "Could not recover the Copilot turn controls",
            description:
              "The saved turn has no cancellation token. Retry to check its status.",
            variant: "destructive",
          });
        }
      }
      const pendingRefinements = data.chat_history.filter(
        (message) =>
          message.sender === "product" &&
          message.turn_id &&
          refineRecordingActionCount(message.content) !== null &&
          !completedTurnIds.has(message.turn_id),
      );
      const pendingRefinementIds = new Map(
        pendingRefinements.map((message) => [
          message.turn_id as string,
          `recording-refinement-${message.turn_id}`,
        ]),
      );
      if (pending) {
        const adopted = startRecoveryPoll(
          data.workflow_copilot_chat_id,
          pending.turn_id,
          undefined,
          undefined,
          true,
          pendingRefinementIds.get(pending.turn_id),
        );
        if (
          adopted &&
          data.question_interactions?.some(
            (item) =>
              item.turn_id === pending.turn_id && item.status === "pending",
          )
        )
          setIsLoading(false);
      }
      for (const [turnId, messageId] of pendingRefinementIds) {
        if (turnId !== pending?.turn_id) {
          startRecoveryPoll(
            data.workflow_copilot_chat_id,
            turnId,
            undefined,
            undefined,
            false,
            messageId,
          );
        }
      }
    },
    [startRecoveryPoll],
  );

  const loadChatInPlace = useCallback(
    async (chatId: string) => {
      if (pendingCanonicalRecovery.current?.acceptChatId) return;
      if (!workflowPermanentId) return;
      const isChatSwitch = workflowCopilotChatIdRef.current !== chatId;
      if (isChatSwitch) {
        streamingAbortController.current?.abort();
        streamingAbortController.current = null;
        inFlightRef.current = false;
        setIsLoading(false);
      }
      resetRecoveryPresentation();
      const loadSeq = beginHistoryLoad();
      discardQueuedPrompt();
      setRejectedTurnIds(new Set());
      setAcceptedTurnIds(new Set());
      setNarrative(EMPTY_NARRATIVE);
      turnSnapshots.current.clear();
      pendingSubmitSnapshot.current = null;
      latestTurnId.current = null;
      repin();
      // This function is the one the identity guards exist to defend against, and it did not
      // guard itself: its response was applied unconditionally, so a user who hit New chat while
      // it was loading got the abandoned chat written back over them. Captured before the fetch
      // and checked before the apply - the apply is what moves this counter, so a legitimate
      // load still passes its own check.
      const navEpochAtStart = chatNavEpochRef.current;
      const selectionAtStart = chatSelectionEpochRef.current;
      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        const response =
          await readCredentialRecoveryHistory<WorkflowCopilotChatHistoryResponse>(
            client,
            workflowPermanentId,
            {
              params: {
                workflow_permanent_id: workflowPermanentId,
                workflow_copilot_chat_id: chatId,
              },
            },
          );
        if (
          chatNavEpochRef.current !== navEpochAtStart ||
          chatSelectionEpochRef.current !== selectionAtStart
        ) {
          return;
        }
        applyHistoryResponse(response.data, !isChatSwitch);
        adoptRecoveredTurns(response.data, response.hadCredentialRecoveryToken);
        // Mark history loaded for this workflow so the mount effect won't reload
        // the latest chat over the one the user just selected.
        historyLoadedForRef.current = workflowPermanentId;
      } catch (error) {
        console.error("Failed to load chat:", error);
        toast({ title: "Failed to load chat", variant: "destructive" });
      } finally {
        endHistoryLoad(loadSeq);
      }
    },
    [
      credentialGetter,
      workflowPermanentId,
      applyHistoryResponse,
      adoptRecoveredTurns,
      resetRecoveryPresentation,
      repin,
      discardQueuedPrompt,
      beginHistoryLoad,
      endHistoryLoad,
    ],
  );

  recoverCredentialTurn.current = (chatId, turnId) =>
    Boolean(startRecoveryPoll(chatId, turnId, undefined, undefined, true));

  const handleSelectHistoryChat = useCallback(
    (chat: WorkflowCopilotChatSummary) => {
      if (acceptUnresolved) return;
      // ADVANCED BEFORE THE EQUALITY RETURN BELOW, and only here. A user selection must
      // invalidate loads already in flight the moment it is made, including a RE-SELECTION OF
      // THE CHAT ALREADY SHOWN - that is how a user undoes a mis-click, and it takes the early
      // return, so an increment placed after it would never run for exactly that case and a
      // pending load of the other chat would still win.
      //
      // Only USER selections move this. loadChatInPlace has three programmatic callers, and a
      // background reload advancing this counter would cancel a selection the user is waiting
      // on - a worse failure than the one this prevents.
      chatSelectionEpochRef.current += 1;
      if (chat.workflow_copilot_chat_id === workflowCopilotChatIdRef.current) {
        return;
      }
      void loadChatInPlace(chat.workflow_copilot_chat_id);
    },
    [acceptUnresolved, loadChatInPlace],
  );

  // Hand the studio's Copilot pane header its History/New-chat controls.
  // Stable wrappers over refs keep the registration limited to value changes.
  const headerHandlersRef = useRef({ handleSelectHistoryChat, handleNewChat });
  headerHandlersRef.current = { handleSelectHistoryChat, handleNewChat };
  // An Accept cannot be cancelled once sent, so no other chat may take over the
  // gate until it settles (New chat stays usable during a turn only to abort it).
  const headerControlsDisabled =
    isLoading || isLoadingHistory || acceptHoldReason !== null;
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
      newChatDisabled: acceptHoldReason !== null,
      navigationLockedReason: acceptHoldReason,
    });
    return () => store.setControls(null);
  }, [
    docked,
    workflowPermanentId,
    workflowCopilotChatId,
    headerControlsDisabled,
    acceptHoldReason,
  ]);

  const applyWorkflowUpdate = useCallback(
    (
      workflow: WorkflowApiResponse,
      options?: WorkflowUpdateOptions,
      reservation?: symbol,
      preservedSettings = options?.settings,
      discardDraft = false,
    ): boolean => {
      if (reservation && !isCopilotTurnCurrent(reservation)) return false;
      try {
        return withCopilotAcceptance(reservation, () => {
          if (!onWorkflowUpdate) return;
          const restored = preservedSettings
            ? restoreWorkflowCopilotSettings(
                workflow,
                preservedSettings,
                options?.settings,
              )
            : null;
          const authoredMetadata =
            !discardDraft && options?.persisted
              ? useWorkflowTitleStore.getState().copilotMetadataEdits[
                  workflow.workflow_permanent_id
                ]?.edits
              : undefined;
          onWorkflowUpdate(
            workflow,
            restored ? { ...options, settings: restored.settings } : options,
          );
          const title = authoredMetadata?.title ?? workflow.title;
          if (!discardDraft && options?.persisted && typeof title === "string")
            useWorkflowTitleStore.getState().setTitle(title, {
              source: "workflow",
            });
          if (authoredMetadata?.description !== undefined)
            useWorkflowTitleStore
              .getState()
              .setDescriptionFromWorkflow(authoredMetadata.description);
          if (discardDraft) {
            const titleStore = useWorkflowTitleStore.getState();
            titleStore.clearCopilotMetadata(workflow.workflow_permanent_id);
            titleStore.trackCopilotMetadata(
              workflow.workflow_permanent_id,
              titleStore.copilotMetadataEdits[workflow.workflow_permanent_id]
                ?.proposal,
            );
            titleStore.setTitle(workflow.title, { source: "workflow" });
            const yaml = useWorkflowYamlEditorStore.getState();
            if (yaml.active)
              useWorkflowYamlEditorStore.setState({
                draft: yaml.entrySnapshot,
                stale: false,
                error: null,
              });
          }
          if (options?.applied && !options.keepLocalGraph) {
            reconcileYamlDraftAfterGraphChange(() => {
              const definition = convert(workflow).workflow_definition;
              return convertToYAML(
                buildWorkflowYamlDocument({
                  workflow,
                  settings:
                    restored?.settings ??
                    options?.settings ??
                    apiWorkflowToSettings(workflow),
                  title:
                    useWorkflowTitleStore.getState().title || workflow.title,
                  description:
                    authoredMetadata?.description !== undefined
                      ? authoredMetadata.description
                      : workflow.description,
                  parameters: definition.parameters,
                  blocks: definition.blocks,
                  definitionVersion: definition.version ?? 1,
                }),
              );
            });
          }
          if (
            authoredMetadata?.title !== undefined ||
            authoredMetadata?.description !== undefined ||
            (restored?.settingsChanged && options?.persisted) ||
            useWorkflowYamlEditorStore.getState().stale
          ) {
            useWorkflowHasChangesStore.getState().setHasChanges(true);
            useWorkflowSnapshotStore.getState().markUserEdit();
          }
        });
      } catch (updateError) {
        // No toast here: the one implementation of onWorkflowUpdate (the studio's, in
        // editor/Workspace.tsx) shows "Update failed" itself and then re-throws so this
        // boolean is honest. Toasting again would stack two failure toasts on one event.
        console.error("Failed to update workflow:", updateError);
        return false;
      }
    },
    [onWorkflowUpdate, isCopilotTurnCurrent],
  );

  const restoreTurnSnapshot = useCallback(
    (entry: TurnSnapshot, reservation?: symbol): RestoreResult => {
      if (reservation && !isCopilotTurnCurrent(reservation))
        return "refused-locked";
      if (!entry.snapshot) return "restored";
      const outcome: { result: RestoreResult } = { result: "refused-locked" };
      const allowed = withCopilotAcceptance(reservation, () => {
        let snapshot = entry.snapshot!;
        if (entry.titlePersisted && !entry.workflowPersisted) {
          const title = useWorkflowTitleStore.getState();
          // The independently saved title survives rollback; its save counter
          // must not make the restored graph dirty.
          snapshot = {
            ...snapshot,
            title: title.title,
            titleHasBeenGenerated: title.titleHasBeenGenerated,
            saveGeneration:
              useWorkflowHasChangesStore.getState().saveGeneration,
          };
        }
        try {
          const { yamlEditor, ...editorSnapshot } = snapshot;
          outcome.result =
            restoreEditorState?.(editorSnapshot) ?? "refused-stale-workflow";
          if (outcome.result === "restored")
            useWorkflowYamlEditorStore.setState(yamlEditor);
        } catch {
          outcome.result = "refused-stale-workflow";
        }
      });
      if (allowed && outcome.result !== "restored")
        toast({
          title: "The draft could not be restored",
          description: "Reload the workflow before continuing.",
          variant: "destructive",
        });
      return allowed ? outcome.result : "refused-locked";
    },
    [restoreEditorState, isCopilotTurnCurrent],
  );

  // A turn that auto-committed a build applies it from the terminal frame. A
  // recovered turn never had that frame, so the editor can still hold the graph
  // from before the drop and a later save would write it back over the commit.
  // Reading canonical is the same thing the reload used to do.
  const fetchChatRow = useCallback(
    async (chatIdOverride?: string | null) => {
      // A caller's own id wins. workflowCopilotChatIdRef is assigned by a passive effect, so a
      // handler that just resolved an id is holding a better answer than the ref can give it for
      // another commit - and reading the ref there returns null and loses the read entirely.
      const chatId = (
        chatIdOverride ?? workflowCopilotChatIdRef.current
      )?.trim();
      if (!chatId) {
        return null;
      }
      const response = await requestWithin(async (signal) => {
        const client = await getClient(credentialGetter, "sans-api-v1");
        if (signal.aborted) throw new Error("Proposal refresh cancelled");
        return client.get<WorkflowCopilotChatHistoryResponse>(
          "/workflow/copilot/chat-history",
          {
            params: { workflow_copilot_chat_id: chatId },
            timeout: ACCEPT_SETTLE_CEILING_MS,
            signal,
          },
        );
      });
      return response.data;
    },
    [credentialGetter],
  );

  const markProposalAccepted = useCallback(
    (ownerTurnId?: string | null) => {
      const owner = ownerTurnId ?? pendingProposalTurnId;
      if (owner) {
        setAcceptedTurnIds((prev) => new Set(prev).add(owner));
      }
      setPendingProposalTurnId(null);
    },
    [pendingProposalTurnId],
  );

  // A follow-up turn that ends without a new draft no longer nulls a bypassed
  // proposal client-side; re-fetch the chat row instead, since the
  // backend (keep_pending_proposal) may have kept it alive server-side.
  // useCallback-stable: handleSend depends on it and is itself a dependency
  // of other effects, so a churning identity here would cascade into them.
  const applyChatRowProposal = useCallback(
    (
      row: WorkflowCopilotChatHistoryResponse,
      // The value of autoAcceptWrites when the READ was issued. A row fetched before a Turn off
      // landed describes the chat before that write, so it may not put auto-accept back on. The
      // read and the apply are separate functions here, so the caller carries the count.
      autoAcceptWritesAtRead?: number,
    ) => {
      if (
        autoAcceptWritesAtRead === undefined ||
        autoAcceptWritesAtRead === autoAcceptWrites.current
      ) {
        setAutoAccept(row.auto_accept ?? false);
      }
      const nextProposal = row.proposed_workflow ?? null;
      if (
        workflowPermanentId &&
        !useWorkflowYamlEditorStore.getState().pendingAccepts[
          workflowPermanentId
        ]
      )
        useWorkflowTitleStore
          .getState()
          .trackCopilotMetadata(
            workflowPermanentId,
            nextProposal
              ? `${row.workflow_copilot_chat_id}:${proposalTokenOf(row.proposed_workflow_metadata ?? null) ?? "legacy"}`
              : null,
          );
      setProposedWorkflow(nextProposal);
      setPendingProposalMetadata(row.proposed_workflow_metadata ?? null);
      setPendingProposalRun(row.proposed_workflow_run ?? null);
      setPendingProposalTurnId((currentTurnId) =>
        nextProposal
          ? (row.proposed_workflow_metadata?.owner_turn_id ??
            getLatestDiffCardTurnIdFromHistory(row.chat_history) ??
            currentTurnId)
          : null,
      );
    },
    [workflowPermanentId],
  );

  const reconcileCanonicalWorkflow = useCallback(
    async (turnReservation?: symbol, signal?: AbortSignal) => {
      if (
        !workflowPermanentId ||
        canonicalRecoveryInFlight.current ||
        !turnReservation ||
        !isCopilotTurnCurrent(turnReservation)
      )
        return;
      const pending = pendingCanonicalRecovery.current;
      if (!pending) return;
      if (pending.workflowPermanentId !== workflowPermanentId) return;
      const store = useWorkflowYamlEditorStore.getState();
      if (
        store.commitInProgress ||
        store.authoringInProgress ||
        (store.copilotAcceptance && store.copilotAcceptance !== turnReservation)
      ) {
        if (pending) pending.waitingForUnlock = true;
        return;
      }
      const reservation = turnReservation;
      if (pending) pending.waitingForUnlock = false;
      canonicalRecoveryInFlight.current = true;
      const generation = recoveryGeneration.current;
      const controller = new AbortController();
      canonicalRecoveryAbort.current = controller;
      const abort = () => controller.abort();
      signal?.addEventListener("abort", abort, { once: true });
      const timeout = setTimeout(
        abort,
        pending.retryAccept
          ? ACCEPT_SETTLE_CEILING_MS
          : CANONICAL_READ_TIMEOUT_MS,
      );
      try {
        const cancelled = new Promise<never>((_, reject) => {
          controller.signal.addEventListener(
            "abort",
            () =>
              reject(
                new Error("Canonical recovery read cancelled or timed out"),
              ),
            { once: true },
          );
          if (signal?.aborted) abort();
        });
        if (pending.draftChoice === "keep" && !pending.savedWorkflow) {
          pendingCanonicalRecovery.current = null;
          return true;
        }
        if (pending.savedWorkflow) {
          if (
            (pending.localDraftConflict || pending.hadGraphEdits) &&
            !pending.draftChoice
          ) {
            pending.localDraftConflict = true;
            return false;
          }
          if (pending.yaml && !useWorkflowYamlEditorStore.getState().active)
            useWorkflowYamlEditorStore.setState({
              active: true,
              ...pending.yaml,
            });
          const applied = applyWorkflowUpdate(
            pending.savedWorkflow,
            {
              persisted: true,
              applied: true,
              keepLocalGraph: pending.draftChoice === "keep",
            },
            reservation,
            pending.draftChoice === "discard"
              ? undefined
              : pending.preservedSettings,
            pending.draftChoice === "discard",
          );
          if (!applied) return false;
          if (!pending.gateAttempt?.wroteNothing)
            markProposalAccepted(pending.savedOwnerTurnId ?? null);
          setProposedWorkflow(null);
          setPendingProposalMetadata(null);
          setPendingProposalRun(null);
          setGateFailure(
            pending.gateAttempt?.wroteNothing
              ? { kind: "changed", ...pending.gateAttempt }
              : null,
          );
          pendingCanonicalRecovery.current = null;
          const chatId = workflowCopilotChatIdRef.current;
          const navEpoch = chatNavEpochRef.current;
          const sendEpoch = sendEpochRef.current;
          const writes = autoAcceptWrites.current;
          void fetchChatRow(chatId)
            .then((row) => {
              if (
                row &&
                composerMountedRef.current &&
                workflowCopilotChatIdRef.current === chatId &&
                chatNavEpochRef.current === navEpoch &&
                sendEpochRef.current === sendEpoch &&
                autoAcceptWrites.current === writes
              )
                setAutoAccept(row.auto_accept ?? false);
            })
            .catch(() => undefined);
          return true;
        }
        if (pending.retryApply) {
          const applied = await Promise.race([cancelled, pending.retryApply()]);
          if (
            !isCopilotTurnCurrent(reservation) ||
            pendingCanonicalRecovery.current !== pending
          )
            return;
          if (applied) pendingCanonicalRecovery.current = null;
          return applied;
        }
        const sameVersion = (canonical: WorkflowApiResponse) => {
          const baseline = pending.baseline;
          return Boolean(
            baseline &&
            baseline.workflow_id === canonical.workflow_id &&
            baseline.version === canonical.version &&
            baseline.modified_at === canonical.modified_at,
          );
        };
        const stillOnRecoveryChat = () =>
          workflowCopilotChatIdRef.current === pending.chatId;
        const clearRecoveryProposal = () => {
          if (!stillOnRecoveryChat()) return;
          setProposedWorkflow(null);
          setPendingProposalMetadata(null);
          setPendingProposalRun(null);
          setPendingProposalTurnId(null);
        };
        let noWriteRow: WorkflowCopilotChatHistoryResponse | null = null;
        const autoAcceptWritesAtRead = autoAcceptWrites.current;
        const response = await Promise.race([
          cancelled,
          (async () => {
            let proposal: WorkflowApiResponse | null | undefined;

            if (pending.acceptChatId) {
              const client = await getClient(credentialGetter, "sans-api-v1");
              if (
                controller.signal.aborted ||
                !isCopilotTurnCurrent(reservation)
              )
                throw new Error("Accept recovery read cancelled");
              const history =
                await readCredentialRecoveryHistory<WorkflowCopilotChatHistoryResponse>(
                  client,
                  workflowPermanentId,
                  {
                    params: { workflow_copilot_chat_id: pending.acceptChatId },
                    signal: controller.signal,
                  },
                );
              if (
                controller.signal.aborted ||
                !isCopilotTurnCurrent(reservation) ||
                pendingCanonicalRecovery.current !== pending
              )
                throw new Error("Accept recovery read cancelled");
              proposal = history.data.proposed_workflow;
              const metadata = history.data.proposed_workflow_metadata;
              const attempt = pending.acceptAttempt;
              const remaining = history.data.proposed_claim_expires_in_seconds;
              const claimInactive =
                remaining === null ||
                (typeof remaining === "number" &&
                  Number.isFinite(remaining) &&
                  remaining <= 0);
              pending.retryAcceptAvailable = Boolean(
                claimInactive &&
                proposal &&
                attempt &&
                metadata &&
                metadata.owner_turn_id === attempt.owner_turn_id &&
                metadata.revision === attempt.revision &&
                metadata.disposition !== "accepting",
              );
              if (claimInactive && pending.gateAttempt?.wroteNothing) {
                noWriteRow = history.data;
                if (proposal) return { data: null, proposal };
                if (proposal === null && stillOnRecoveryChat()) {
                  applyChatRowProposal(history.data, autoAcceptWritesAtRead);
                  setGateFailure({ kind: "changed", ...pending.gateAttempt });
                }
              }
              if (pending.retryAccept && pending.retryAcceptAvailable) {
                pending.retryAccept = false;
                try {
                  const applied = await client.post<WorkflowApiResponse>(
                    "/workflow/copilot/apply-proposed-workflow",
                    {
                      workflow_copilot_chat_id: pending.acceptChatId,
                      auto_accept: pending.gateAttempt?.alwaysAccept ?? false,
                      owner_turn_id: attempt?.owner_turn_id,
                      revision: attempt?.revision,
                    },
                    {
                      timeout: ACCEPT_SETTLE_CEILING_MS,
                      signal: controller.signal,
                    },
                  );
                  if (
                    !controller.signal.aborted &&
                    isCopilotTurnCurrent(reservation) &&
                    pendingCanonicalRecovery.current === pending
                  ) {
                    pending.savedWorkflow = applied.data;
                    pending.savedOwnerTurnId = attempt?.owner_turn_id ?? null;
                  }
                } catch {
                  /* A retry cannot settle the original request's outcome. */
                }
                return { data: null, proposal };
              }
              if (!claimInactive || proposal !== null)
                return { data: null, proposal };
            }
            const client = await getClient(credentialGetter);
            const readCanonical = async () => {
              if (
                controller.signal.aborted ||
                !isCopilotTurnCurrent(reservation)
              )
                throw new Error("Canonical recovery read cancelled");
              return client.get<WorkflowApiResponse>(
                `/workflows/${workflowPermanentId}`,
                {
                  timeout: CANONICAL_READ_TIMEOUT_MS,
                  signal: controller.signal,
                },
              );
            };
            const canonical = await readCanonical();
            if (
              pending.acceptChatId &&
              (typeof canonical.data.workflow_id !== "string" ||
                proposal === undefined)
            )
              throw new Error("Accept recovery returned incomplete evidence");
            return { ...canonical, proposal };
          })(),
        ]);
        if (
          controller.signal.aborted ||
          !isCopilotTurnCurrent(reservation) ||
          generation !== recoveryGeneration.current ||
          pendingCanonicalRecovery.current !== pending ||
          getSaveData()?.workflow.workflow_permanent_id !== workflowPermanentId
        )
          return;
        if (
          (pending.awaitingTurnId || pending.terminalConfirmed === false) &&
          !pending.terminalConfirmed
        )
          return false;
        if (noWriteRow && response.proposal && pending.gateAttempt) {
          if (stillOnRecoveryChat()) {
            applyChatRowProposal(noWriteRow, autoAcceptWritesAtRead);
            setGateFailure({ kind: "accept", ...pending.gateAttempt });
          }
          pendingCanonicalRecovery.current = null;
          return true;
        }
        if (pending.acceptChatId) {
          if (response.proposal !== null) return false;
        }
        if (!response.data) return false;
        const canonicalUnchanged = sameVersion(response.data);
        const baseline = pending.baseline;
        const titleOnlyAdvance =
          baseline &&
          baseline.title !== response.data.title &&
          hashKey([
            {
              ...baseline,
              title: response.data.title,
              version: response.data.version,
              modified_at: response.data.modified_at,
            },
          ]) === hashKey([response.data]);
        if (pending.acceptChatId && pending.hadGraphEdits) {
          pending.savedWorkflow = response.data;
          pending.localDraftConflict = true;
          return false;
        }
        if (
          (pending.hadLocalEdits ||
            (pending.gateAttempt?.wroteNothing &&
              useWorkflowHasChangesStore.getState().hasChanges)) &&
          !pending.draftChoice &&
          !pending.cancellationRequested &&
          (pending.acceptChatId ||
            !canonicalUnchanged ||
            (pending.restoreRollback && pending.rollback?.snapshot))
        ) {
          pending.localDraftConflict = true;
          return false;
        }
        if (
          !pending.acceptChatId &&
          (titleOnlyAdvance ||
            (canonicalUnchanged && pending.draftChoice !== "discard"))
        ) {
          if (!pending.terminalConfirmed) return false;
          if (titleOnlyAdvance) {
            if (pending.rollback) pending.rollback.titlePersisted = true;
            onWorkflowPersisted?.(workflowPermanentId);
            withCopilotAcceptance(reservation, () => {
              const titles = useWorkflowTitleStore.getState();
              if (pending.rollback?.snapshot)
                titles.restoreTitle(
                  pending.rollback.snapshot.title,
                  pending.rollback.snapshot.titleHasBeenGenerated,
                );
              titles.setTitleFromCopilotIfDefault(response.data.title);
            });
          }
          if (
            pending.restoreRollback &&
            pending.rollback?.snapshot &&
            restoreTurnSnapshot(pending.rollback, reservation) !== "restored"
          )
            return;
          if (pending.restoreRollback) clearRecoveryProposal();
          pendingCanonicalRecovery.current = null;
          return true;
        }
        if (!canonicalUnchanged) {
          if (pending.rollback) pending.rollback.workflowPersisted = true;
          onWorkflowPersisted?.(workflowPermanentId);
        }
        if (pending?.yaml && !useWorkflowYamlEditorStore.getState().active) {
          useWorkflowYamlEditorStore.setState({
            active: true,
            ...pending.yaml,
          });
        }
        if (
          !applyWorkflowUpdate(
            response.data,
            { persisted: true, applied: true, fresh: true },
            reservation,
            pending.draftChoice === "discard"
              ? undefined
              : pending.preservedSettings,
            pending.draftChoice === "discard",
          )
        ) {
          if (pending) pending.waitingForUnlock = true;
          return;
        }
        if (pending) {
          if (
            !canonicalUnchanged &&
            pending.rollback?.snapshot &&
            (pending.rollback.titlePersisted ||
              pending.rollback.workflowPersisted)
          )
            toast({
              title: "The draft could not be restored",
              description:
                "The Copilot change was saved on the server. The saved workflow was loaded.",
              variant: "destructive",
            });
        }
        clearRecoveryProposal();
        if (pending.acceptChatId && stillOnRecoveryChat())
          setGateFailure(
            pending.gateAttempt?.wroteNothing
              ? { kind: "changed", ...pending.gateAttempt }
              : null,
          );
        pendingCanonicalRecovery.current = null;
        return true;
      } catch (error) {
        console.warn("Failed to re-read the workflow after recovery:", error);
        if (
          isCopilotTurnCurrent(reservation) &&
          pendingCanonicalRecovery.current === pending
        ) {
          toast({
            title: "Could not check the saved Copilot change",
            description:
              "Your draft is retained. Reload to check the saved workflow before saving.",
            variant: "destructive",
          });
        }
      } finally {
        clearTimeout(timeout);
        signal?.removeEventListener("abort", abort);
        if (canonicalRecoveryAbort.current === controller) {
          canonicalRecoveryAbort.current = null;
          canonicalRecoveryInFlight.current = false;
        }
      }
    },
    [
      applyWorkflowUpdate,
      credentialGetter,
      getSaveData,
      workflowPermanentId,
      isCopilotTurnCurrent,
      restoreTurnSnapshot,
      onWorkflowPersisted,
      fetchChatRow,
      markProposalAccepted,
      applyChatRowProposal,
    ],
  );
  reconcileCanonicalWorkflowRef.current = reconcileCanonicalWorkflow;

  useEffect(
    () =>
      useWorkflowYamlEditorStore.subscribe((state, previous) => {
        if (
          (previous.commitInProgress ||
            previous.copilotAcceptance ||
            previous.authoringInProgress) &&
          !state.commitInProgress &&
          (!state.copilotAcceptance ||
            state.copilotAcceptance === copilotReservation.current) &&
          !state.authoringInProgress &&
          pendingCanonicalRecovery.current?.waitingForUnlock
        ) {
          const reserved = [...recoveryPolls.current.values()].find((poll) =>
            poll.isReserved?.(),
          );
          if (reserved) reserved.retry?.();
        }
      }),
    [],
  );

  // Records the accepted turn (for the "Applied changes" relabel) before
  // clearing the pending-gate handle.
  // Takes an explicit owner when the caller held one: the `saved` gate survives a hydration
  // that clears pendingProposalTurnId, and without its own copy a confirmed save would clear
  // the gate with no turn to mark - a save that landed, reported as nothing.
  const handleAcceptWorkflow = (alwaysAccept: boolean = false) => {
    const chatKey = workflowCopilotChatIdRef.current?.trim() ?? "";
    const workflow = proposedWorkflow;
    if (!workflow || acceptUnresolved) return;
    const accepting = acceptWorkflow(workflow, alwaysAccept);
    // A refused duplicate must not replace the pending accept that Turn off needs to wait for.
    acceptsInFlight.current.set(
      chatKey,
      Promise.allSettled([
        acceptsInFlight.current.get(chatKey),
        accepting,
      ]).then(() => undefined),
    );
    return accepting;
  };

  const acceptWorkflow = async (
    workflow: WorkflowApiResponse,
    alwaysAccept: boolean,
  ) => {
    if (!workflowPermanentId) return;
    const acceptance = reserveCopilotTurn("accept");
    if (!acceptance) return;
    copilotReservation.current = acceptance;
    let recovering = false;
    setGateFailure(null);
    setIsAccepting(true);
    try {
      const saveData = saveDataGetter.current();
      const baseline = structuredClone(saveData?.workflow);
      const acceptAttempt = pendingProposalMetadata
        ? {
            owner_turn_id: pendingProposalMetadata.owner_turn_id,
            revision: pendingProposalMetadata.revision,
            disposition: pendingProposalMetadata.disposition,
          }
        : null;
      const preservedSettings =
        structuredClone(saveData?.settings) ??
        (proposalPreservedSettings.current?.workflow === workflow
          ? proposalPreservedSettings.current.settings
          : undefined) ??
        (pendingProposalTurnId
          ? turnSnapshots.current.get(pendingProposalTurnId)?.settings
          : undefined);
      let chatId = workflowCopilotChatIdRef.current?.trim() || null;
      // A pending accept belongs to its original chat, even if the pane switches or opens New chat.
      const startedOnChatId = chatId;
      const stillOnAcceptedChat = () => {
        const shown = workflowCopilotChatIdRef.current?.trim() || null;
        return shown === startedOnChatId || shown === chatId;
      };
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

      if (!isCopilotTurnCurrent(acceptance)) return;
      if (!chatId) {
        setGateFailure({
          kind: "accept",
          alwaysAccept,
          token: proposalTokenOf(pendingProposalMetadata),
          wroteNothing: false,
        });
        toast({
          title: "Accept failed",
          description:
            "Copilot could not verify the current proposal. Please try again.",
          variant: "destructive",
        });
        return;
      }

      const snapshot: CopilotAcceptSnapshot & CanonicalRecovery = {
        workflowPermanentId,
        chatId,
        acceptChatId: chatId,
        acceptAttempt,
        gateAttempt: {
          alwaysAccept,
          token: proposalTokenOf(pendingProposalMetadata),
          wroteNothing: false,
        },
        baseline,
        preservedSettings,
        hadGraphEdits:
          useWorkflowTitleStore.getState().copilotMetadataEdits[
            workflowPermanentId
          ]?.graphEdited === true,
        terminalConfirmed: true,
        waitingForUnlock: false,
        yaml: useWorkflowYamlEditorStore.getState().active
          ? {
              draft: useWorkflowYamlEditorStore.getState().draft,
              entrySnapshot:
                useWorkflowYamlEditorStore.getState().entrySnapshot,
            }
          : null,
      };
      if (
        !storePendingAccept(workflowPermanentId, {
          chatId,
          acceptAttempt,
          alwaysAccept,
        })
      ) {
        toast({
          title: "Accept unavailable",
          description:
            "Browser storage is unavailable. Free space or reload before accepting.",
          variant: "destructive",
        });
        return;
      }
      pendingCanonicalRecovery.current = snapshot;
      useWorkflowYamlEditorStore.setState((state) => ({
        pendingAccepts: {
          ...state.pendingAccepts,
          [workflowPermanentId]: { reservation: acceptance, snapshot },
        },
      }));
      try {
        const response = await requestWithin(async (signal) => {
          const client = await getClient(credentialGetter, "sans-api-v1");
          if (signal.aborted || !isCopilotTurnCurrent(acceptance))
            throw new Error("Accept request cancelled");
          return client.post<WorkflowApiResponse>(
            "/workflow/copilot/apply-proposed-workflow",
            {
              workflow_copilot_chat_id: chatId,
              auto_accept: alwaysAccept,
              owner_turn_id: acceptAttempt?.owner_turn_id ?? null,
              revision: acceptAttempt?.revision ?? null,
            } as WorkflowCopilotApplyProposedWorkflowRequest,
            { timeout: ACCEPT_SETTLE_CEILING_MS, signal },
          );
        }).finally(() => {
          // Queries outlive the submitting editor, including on a lost response.
          for (const queryKey of [
            ["workflow", workflowPermanentId],
            ["workflows"],
            ["block-scripts", workflowPermanentId],
          ]) {
            void queryClient.invalidateQueries({ queryKey });
          }
        });
        onWorkflowPersisted?.(workflowPermanentId);
        if (!isCopilotTurnCurrent(acceptance)) return;
        // persisted=true loads as clean baseline; without it, Save would create a duplicate version.
        if (
          snapshot.hadGraphEdits ||
          !applyWorkflowUpdate(
            response.data,
            {
              persisted: true,
              applied: true,
              fresh: true,
            },
            acceptance,
            preservedSettings,
          )
        ) {
          noteAutoAcceptWrite();
          snapshot.localDraftConflict = snapshot.hadGraphEdits;
          snapshot.savedWorkflow = response.data;
          snapshot.savedOwnerTurnId = pendingProposalTurnId;
          recovering = true;
          setGateFailure({
            kind: "saved",
            savedWorkflow: response.data,
            ownerTurnId: pendingProposalTurnId,
            ...snapshot.gateAttempt!,
          });
          startRecoveryPoll(chatId, null, undefined, acceptance);
          return;
        }
        if (!stillOnAcceptedChat()) {
          return;
        }
        markProposalAccepted();
        setProposedWorkflow(null);
        setPendingProposalMetadata(null);
        setPendingProposalRun(null);
        if (alwaysAccept) {
          setAutoAcceptFromWrite(true);
        } else {
          // A plain Accept writes auto_accept=false on the row, so reads taken before it are stale too.
          noteAutoAcceptWrite();
        }
        if (alwaysAccept !== autoAcceptRef.current) {
          // Apply writes auto-accept best-effort after creating the version, so show what the chat row kept.
          void resyncProposalFromChatRow();
        }
      } catch (error) {
        const rejection = definitiveAcceptRejection(error);
        if (
          rejection &&
          (!isCopilotTurnCurrent(acceptance) || getErrorStatus(error) === 404)
        ) {
          snapshot.definitiveRejection = rejection;
          finishCopilotAcceptance(acceptance);
          return;
        }
        if (!isCopilotTurnCurrent(acceptance)) return;
        snapshot.gateAttempt!.wroteNothing = applyWroteNothing(
          getErrorStatus(error),
        );
        if (snapshot.gateAttempt!.wroteNothing) {
          const settled = await reconcileCanonicalWorkflow(acceptance);
          if (rejection)
            toast({
              title: "Accept failed",
              description: rejection,
              variant: "destructive",
            });
          if (settled) return;
        }
        recovering = true;
        pendingCanonicalRecovery.current = snapshot;
        setGateFailure((current) =>
          current?.kind === "changed"
            ? current
            : { kind: "recover", ...snapshot.gateAttempt! },
        );
        startRecoveryPoll(chatId, null, undefined, acceptance);
      }
    } finally {
      if (isCopilotOwnerCurrent(acceptance)) setIsAccepting(false);
      if (!recovering && isCopilotTurnCurrent(acceptance)) {
        pendingCanonicalRecovery.current = null;
        finishCopilotAcceptance(acceptance);
        if (copilotReservation.current === acceptance)
          copilotReservation.current = null;
      }
    }
  };

  // Try again may only re-send the proposal that failed. A reload that swapped
  // in a different one, or a legacy proposal with no token to prove it did not,
  // needs the user to review what is shown and Accept it fresh.
  const gateFailureKind =
    gateFailure?.kind === "accept" &&
    gateFailure.token !== proposalTokenOf(pendingProposalMetadata)
      ? "changed"
      : (gateFailure?.kind ?? null);
  const gateFailureRetryable =
    gateFailureKind === "reload" ||
    gateFailureKind === "recover" ||
    gateFailureKind === "saved" ||
    (gateFailureKind === "accept" &&
      gateFailure?.kind === "accept" &&
      gateFailure.token !== null);

  const resyncGeneration = useRef(0);
  const retryGateFailure = () => {
    if (!gateFailure) return;
    if (gateFailure.kind === "accept") {
      void handleAcceptWorkflow(gateFailure.alwaysAccept);
    } else if (outstandingAccept || pendingCanonicalRecovery.current) {
      recoveryControls?.retry();
    } else {
      void resyncProposalFromChatRow().then((row) => {
        if (row) setGateFailure(null);
      });
    }
  };

  const handleRejectWorkflow = async (): Promise<boolean> => {
    if (acceptHoldReason !== null) {
      toast({ title: acceptHoldReason, variant: "destructive" });
      return false;
    }
    const rejectSendEpoch = sendEpochRef.current;
    const rejectNavEpoch = chatNavEpochRef.current;
    setGateFailure(null);
    const acceptance = reserveCopilotTurn();
    if (!acceptance) return false;
    copilotReservation.current = acceptance;
    let recovering = false;
    try {
      if (
        !(await clearProposedWorkflow(false)) ||
        !isCopilotTurnCurrent(acceptance) ||
        sendEpochRef.current !== rejectSendEpoch ||
        chatNavEpochRef.current !== rejectNavEpoch
      ) {
        return false;
      }
      // The staged proposal was rendered onto the canvas mid-turn (via
      // WORKFLOW_DRAFT). Reject must revert the canvas to the pre-submit
      // canvas state captured client-side at submit time.
      const turnId =
        pendingProposalTurnId ??
        latestTurnId.current ??
        getLatestDiffCardTurnId(messages);
      const entry = turnId ? turnSnapshots.current.get(turnId) : null;
      if (entry?.snapshot) {
        if (restoreTurnSnapshot(entry, acceptance) !== "restored") {
          recovering = Boolean(
            createCanonicalRecovery(acceptance, {
              rollback: entry,
              restoreRollback: true,
              terminalConfirmed: true,
            }),
          );
          if (recovering)
            startRecoveryPoll(
              workflowCopilotChatIdRef.current,
              null,
              crypto.randomUUID(),
              acceptance,
            );
          return false;
        }
      }
      if (turnId) {
        setRejectedTurnIds((prev) => new Set(prev).add(turnId));
      }
      setProposedWorkflow(null);
      setPendingProposalMetadata(null);
      setPendingProposalRun(null);
      setPendingProposalTurnId(null);
      return true;
    } finally {
      if (!recovering && !pendingCanonicalRecovery.current?.acceptChatId) {
        finishCopilotAcceptance(acceptance);
        if (copilotReservation.current === acceptance)
          copilotReservation.current = null;
      }
    }
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
    const response =
      await readCredentialRecoveryHistory<WorkflowCopilotChatHistoryResponse>(
        client,
        workflowPermanentId,
        {
          params: { workflow_permanent_id: workflowPermanentId },
        },
      );
    const latestChatId = response.data.workflow_copilot_chat_id ?? null;
    setWorkflowCopilotChatId(latestChatId);
    return latestChatId;
  };

  const uploadAttachment = useCallback(
    async (file: File) => {
      if (file.size > attachmentSizeLimit(file)) {
        toast({
          variant: "destructive",
          title: "File too large",
          description: isVideoAttachment(file)
            ? `${file.name} exceeds the 30MB limit. Shorten or compress the clip, or attach ordered screenshots.`
            : `${file.name} exceeds the 10MB limit.`,
        });
        setPendingAttachments((prev) => [
          ...prev,
          {
            localId: crypto.randomUUID(),
            filename: file.name,
            status: "error",
            error: isVideoAttachment(file) ? "over 30MB" : "over 10MB",
          },
        ]);
        return;
      }
      if (isVideoAttachment(file)) {
        toast({
          title: "Use a non-sensitive video",
          description:
            "Videos can be up to five minutes and 30MB. Remove passwords, one-time codes, API keys, and other secrets.",
        });
      }
      const uploadingCount = pendingAttachmentsRef.current.filter(
        (item) => item.status === "uploading",
      ).length;
      if (
        attachmentsRef.current.length + uploadingCount >=
        ATTACHMENT_COUNT_LIMIT
      ) {
        toast({
          variant: "destructive",
          title: "Too many files",
          description: `A message can carry up to ${ATTACHMENT_COUNT_LIMIT} files.`,
        });
        return;
      }
      const localId = crypto.randomUUID();
      setPendingAttachments((prev) => [
        ...prev,
        { localId, filename: file.name, status: "uploading" },
      ]);
      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        const formData = new FormData();
        formData.append("file", file);
        const response = await client.post<
          FormData,
          { data: { file_id: string } }
        >("/upload_file", formData, {
          headers: { "Content-Type": "multipart/form-data" },
        });
        if (!composerMountedRef.current || pageHiddenRef.current) {
          // Nothing can reach this file: the composer is gone, or the page was left while the upload
          // was still running, so page-exit cleanup never saw an id for it.
          setPendingAttachments((prev) =>
            prev.filter((item) => item.localId !== localId),
          );
          const abandoned: CopilotAttachedFile = {
            file_id: response.data.file_id,
            filename: file.name,
            size_bytes: file.size,
            available: true,
          };
          void removeUnsentUploadRef
            .current(abandoned.file_id)
            .then((deleted) => {
              // A delete that never landed leaves the upload reachable only through a chip, so a
              // page that comes back gets one rather than losing the file.
              if (!deleted && composerMountedRef.current) {
                returnFilesToTrayRef.current([abandoned]);
              }
            });
          return;
        }
        setPendingAttachments((prev) =>
          prev.filter((item) => item.localId !== localId),
        );
        setAttachments((prev) => [
          ...prev.filter((item) => item.file_id !== response.data.file_id),
          {
            file_id: response.data.file_id,
            filename: file.name,
            size_bytes: file.size,
            available: true,
          },
        ]);
      } catch (error) {
        setPendingAttachments((prev) =>
          prev.map((item) =>
            item.localId === localId
              ? {
                  ...item,
                  status: "error",
                  error: "upload failed",
                }
              : item,
          ),
        );
        toast({
          variant: "destructive",
          title: "Upload failed",
          description: `Could not attach ${file.name}. Try again.`,
        });
      }
    },
    [credentialGetter],
  );

  const removeUnsentUpload = useCallback(
    async (fileId: string): Promise<boolean> => {
      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        await client.delete(`/files/${fileId}`);
        return true;
      } catch (error) {
        console.warn("Failed to delete removed attachment:", error);
        return false;
      }
    },
    [credentialGetter],
  );

  const removeUnsentUploadRef = useRef(removeUnsentUpload);
  removeUnsentUploadRef.current = removeUnsentUpload;
  const returnFilesToTrayRef = useRef(returnFilesToTray);
  returnFilesToTrayRef.current = returnFilesToTray;

  // Leaving the page skips React cleanup and cancels in-flight requests, so pagehide sends a keepalive
  // delete the browser completes after the page is gone; unmount covers in-app navigation.
  useEffect(() => {
    composerMountedRef.current = true;
    // The same Set for the component's lifetime; ids posted later are still visible through it.
    const posted = postedFileIds.current;
    const inFlight = inFlightFileIds.current;
    const reclaimed = reclaimingFiles.current;
    const deletions = new Map<string, Promise<boolean>>();
    const abandonedFiles = () => {
      const byId = new Map<string, CopilotAttachedFile>();
      for (const attached of [
        ...attachmentsRef.current,
        ...(queuedPromptRef.current?.attachments ?? []),
        ...[...unpostedSendsRef.current].flat(),
      ]) {
        const fileId = attached.file_id;
        if (
          posted.has(fileId) ||
          inFlight.has(fileId) ||
          reclaimed.has(fileId)
        ) {
          continue;
        }
        byId.set(fileId, attached);
      }
      return [...byId.values()];
    };
    const onPageHide = () => {
      pageHiddenRef.current = true;
      for (const attached of abandonedFiles()) {
        reclaimed.set(attached.file_id, attached);
        deletions.set(
          attached.file_id,
          deleteUploadedFileOnPageExit(attached.file_id),
        );
      }
    };
    // A back/forward-cached page can be evicted without ever running cleanup, so pagehide deletes even
    // when the page may return; if it does, those files leave the tray instead of naming deleted uploads.
    const onPageShow = (event: PageTransitionEvent) => {
      pageHiddenRef.current = false;
      if (!event.persisted || deletions.size === 0) {
        return;
      }
      const attempts = [...deletions.entries()];
      deletions.clear();
      void Promise.all(
        attempts.map(async ([fileId, attempt]) => ({
          fileId,
          deleted: await attempt,
        })),
      ).then((results) => {
        // A delete the browser never completed leaves the upload reachable, so its file comes back
        // to the tray, even if a send in the meantime cleared it, and can be tried again.
        const kept: CopilotAttachedFile[] = [];
        for (const { fileId, deleted } of results) {
          if (deleted) {
            continue;
          }
          const attached = reclaimed.get(fileId);
          reclaimed.delete(fileId);
          if (attached) {
            kept.push(attached);
          }
        }
        if (kept.length > 0 && composerMountedRef.current) {
          returnFilesToTrayRef.current(kept);
        }
        const gone = new Set(
          results.filter((result) => result.deleted).map((r) => r.fileId),
        );
        if (gone.size === 0 || !composerMountedRef.current) {
          return;
        }
        setAttachments((current) =>
          current.filter((attached) => !gone.has(attached.file_id)),
        );
        // Written through the ref and setter rather than updateQueuedPrompt so this effect keeps no
        // dependencies: its cleanup deletes uploads and must run only on unmount.
        const queued = queuedPromptRef.current;
        if (
          queued?.attachments?.some((attached) => gone.has(attached.file_id))
        ) {
          const kept = queued.attachments.filter(
            (attached) => !gone.has(attached.file_id),
          );
          const next = { ...queued, attachments: kept };
          queuedPromptRef.current = next;
          setQueuedPrompt(next);
          setMessages((prev) =>
            prev.map((message) =>
              message.id === queued.id
                ? {
                    ...message,
                    attachedFiles: kept.length > 0 ? kept : undefined,
                  }
                : message,
            ),
          );
        }
        toast({
          title: "Attachments removed",
          description:
            "Files staged before you left the page were deleted. Attach them again to send.",
        });
      });
    };
    window.addEventListener("pagehide", onPageHide);
    window.addEventListener("pageshow", onPageShow);
    return () => {
      window.removeEventListener("pagehide", onPageHide);
      window.removeEventListener("pageshow", onPageShow);
      composerMountedRef.current = false;
      for (const attached of abandonedFiles()) {
        void removeUnsentUploadRef.current(attached.file_id);
      }
    };
  }, []);

  const uploadDictationAudio = useCallback(
    async (
      audioBlob: Blob,
      reservation: symbol,
    ): Promise<WorkflowCopilotAudioUploadResponse> => {
      if (!workflowPermanentId) {
        throw new Error("Missing workflow permanent ID for audio upload.");
      }

      const client = await getClient(credentialGetter, "sans-api-v1");
      if (!isCopilotTurnCurrent(reservation))
        throw new Error("Dictation upload cancelled");
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
      if (!isCopilotTurnCurrent(reservation)) return response.data;
      setWorkflowCopilotChatId(response.data.workflow_copilot_chat_id);
      workflowCopilotChatIdRef.current = response.data.workflow_copilot_chat_id;
      return response.data;
    },
    [credentialGetter, workflowPermanentId, isCopilotTurnCurrent],
  );

  const rateAssistantTurn = useCallback(
    async (
      localMessageId: string,
      target: { messageId?: string; turnId?: string | null },
      rating: WorkflowCopilotMessageFeedbackRating | null,
      reason?: string,
    ) => {
      const chatId = workflowCopilotChatIdRef.current?.trim();
      if (!chatId) {
        throw new Error("No chat to rate");
      }
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response =
        await client.post<WorkflowCopilotMessageFeedbackResponse>(
          "/workflow/copilot/message-feedback",
          {
            workflow_copilot_chat_id: chatId,
            workflow_copilot_chat_message_id: target.messageId ?? null,
            turn_id: target.turnId ?? null,
            rating,
            reason: reason ?? null,
          },
        );
      setMessages((prev) =>
        prev.map((message) =>
          message.id === localMessageId
            ? {
                ...message,
                messageId: response.data.workflow_copilot_chat_message_id,
                feedback: response.data.feedback,
              }
            : message,
        ),
      );
    },
    [credentialGetter],
  );

  const resyncProposalFromChatRow = useCallback(async () => {
    // Navigation stays open during a plain reload, so its result belongs only to
    // the chat that started it.
    const chatId = workflowCopilotChatIdRef.current;
    const sendEpoch = sendEpochRef.current;
    const editor = useWorkflowYamlEditorStore.getState().editorOwner;
    const recoveryEpoch = recoveryGeneration.current;
    const autoAcceptWritesAtRead = autoAcceptWrites.current;
    // Try again on a `reload` with no attempt neither locks the card nor sets `isAccepting`, so
    // two of these can be in flight at once. The chat id and send epoch below are unchanged
    // between them, so only call identity separates a late answer from the current one - the same
    // guard `reconcileFailedAccept` carries, for the same reason.
    const generation = (resyncGeneration.current += 1);
    const superseded = () =>
      resyncGeneration.current !== generation ||
      !composerMountedRef.current ||
      recoveryGeneration.current !== recoveryEpoch ||
      useWorkflowYamlEditorStore.getState().editorOwner !== editor ||
      Boolean(editor && !editor.active);
    try {
      const row = await fetchChatRow();
      if (
        superseded() ||
        workflowCopilotChatIdRef.current !== chatId ||
        sendEpochRef.current !== sendEpoch
      ) {
        return null;
      }
      if (row) {
        restoreAcceptFromHistory.current(row);
        applyChatRowProposal(row, autoAcceptWritesAtRead);
        // This path does NOT clear staleCanvas. Only a persisted refresh the server gave us in
        // the same act does - applyWorkflowUpdate requires `persisted` AND `fresh` - because a
        // surviving proposal is not evidence the canvas is current.
      }
      return row;
    } catch (error) {
      console.error("Failed to resync pending proposal:", error);
      if (
        !superseded() &&
        workflowCopilotChatIdRef.current === chatId &&
        sendEpochRef.current === sendEpoch
      ) {
        if (!pendingCanonicalRecovery.current?.acceptChatId)
          setGateFailure({ kind: "reload", attempt: null });
      }
      return null;
    }
  }, [applyChatRowProposal, fetchChatRow]);

  // Unlike `resyncProposalFromChatRow`, a failed read here must not raise the `reload` gate or
  // overwrite the fresher proposal the terminal frame already carried.
  const backfillProposalRunFacts = useCallback(
    async (
      ownerTurnId: string | null,
      chatId: string,
      workflowRunId: string,
    ) => {
      const sendEpoch = sendEpochRef.current;
      const navEpoch = chatNavEpochRef.current;
      try {
        const row = await fetchChatRow(chatId);
        if (
          !row?.proposed_workflow_run ||
          chatNavEpochRef.current !== navEpoch ||
          sendEpochRef.current !== sendEpoch ||
          (row.proposed_workflow_metadata?.owner_turn_id ?? null) !==
            ownerTurnId ||
          row.proposed_workflow_run.workflow_run_id !== workflowRunId
        ) {
          return;
        }
        setPendingProposalRun(row.proposed_workflow_run);
      } catch (error) {
        console.error("Failed to backfill proposal run facts:", error);
      }
    },
    [fetchChatRow],
  );

  const clearProposedWorkflow = async (
    autoAcceptValue: boolean,
  ): Promise<boolean> => {
    const reservation = copilotReservation.current;
    if (!reservation || !isCopilotTurnCurrent(reservation)) return false;
    // Resolves false when the pane switched to a chat this write did not touch, so callers leave it alone.
    // A pane still resolving its chat id reads null, or either id, until the next render; that is no switch.
    const startingChatId = workflowCopilotChatIdRef.current?.trim() || null;
    const clearProposalByChatId = async (chatId: string): Promise<boolean> => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      if (!isCopilotTurnCurrent(reservation)) return false;
      await client.post<WorkflowCopilotClearProposedWorkflowRequest>(
        "/workflow/copilot/clear-proposed-workflow",
        {
          workflow_copilot_chat_id: chatId,
          auto_accept: autoAcceptValue,
          owner_turn_id: pendingProposalMetadata?.owner_turn_id ?? null,
          revision: pendingProposalMetadata?.revision ?? null,
        } as WorkflowCopilotClearProposedWorkflowRequest,
      );
      if (!isCopilotTurnCurrent(reservation)) return false;
      const shownChatId = workflowCopilotChatIdRef.current?.trim() || null;
      if (shownChatId !== chatId && shownChatId !== startingChatId) {
        return false;
      }
      setAutoAcceptFromWrite(autoAcceptValue);
      return true;
    };

    let chatId = startingChatId;
    if (!chatId) {
      try {
        chatId = await fetchLatestChatId();
      } catch (resolveError) {
        console.error(
          "Failed to resolve chat ID before clearing proposal:",
          resolveError,
        );
        return false;
      }
    }

    if (!chatId) {
      return false;
    }

    try {
      return await clearProposalByChatId(chatId);
    } catch (error) {
      if (!isCopilotTurnCurrent(reservation)) return false;
      const status = getErrorStatus(error);
      // A known chat must not fall back here: the latest chat can be someone else's row, and
      // clearing it deletes that chat's pending review. Retry only when this clear began before
      // the pane had a chat ID to name.
      if (status === 404 && !startingChatId) {
        try {
          const refreshedChatId = await fetchLatestChatId();
          if (refreshedChatId && refreshedChatId !== chatId) {
            return await clearProposalByChatId(refreshedChatId);
          }
        } catch (retryError) {
          console.error("Retry to clear proposed workflow failed:", retryError);
        }
      }
      if (status === 409) {
        await resyncProposalFromChatRow();
        return false;
      }
      console.error("Failed to clear proposed workflow:", error);
      toast({
        title: "Copilot update failed",
        description: autoAcceptValue
          ? "Agent was applied, but auto-accept did not update."
          : "Failed to clear copilot proposal. Please try again.",
        variant: "destructive",
      });
      return false;
    }
  };

  const handleReviewWorkflow = (workflow: WorkflowApiResponse) => {
    onReviewWorkflow?.(
      workflow,
      () => {
        setProposedWorkflow(null);
        setPendingProposalMetadata(null);
        setPendingProposalRun(null);
        setPendingProposalTurnId(null);
      },
      handleRejectWorkflow,
    );
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
      discardQueuedPrompt();
      setWorkflowCopilotChatId(null);
      setProposedWorkflow(null);
      setPendingProposalMetadata(null);
      setPendingProposalRun(null);
      setPendingProposalTurnId(null);
      setAutoAccept(false);
      setWorkPlan([]);
      setNarrative(EMPTY_NARRATIVE);
      historyLoadedForRef.current = null;
      return;
    }

    if (historyLoadedForRef.current === workflowPermanentId) {
      return;
    }
    if (
      deferredStartupClaim.current?.workflowPermanentId !== workflowPermanentId
    )
      deferredStartupClaim.current = null;
    const isWorkflowSwitch = historyLoadedForRef.current !== null;
    // A poll armed against the outgoing chat must not apply history to the new
    // workflow. The state reset is only needed after a workflow was loaded;
    // writing fresh empty arrays on first mount can restart this async effect
    // before its history request records completion.
    stopRecoveryPolls();
    const stored = readStoredAccept(workflowPermanentId);
    const parked =
      useWorkflowYamlEditorStore.getState().pendingAccepts[workflowPermanentId];
    if (parked) {
      canonicalRecoveriesByWorkflow.delete(workflowPermanentId);
      const editor = useWorkflowYamlEditorStore.getState().editorOwner;
      useWorkflowYamlEditorStore.setState((state) => ({
        copilotAcceptance: parked.reservation,
        lockKind: state.commitInProgress ? state.lockKind : "copilot",
      }));
      copilotReservation.current = parked.reservation;
      turnOwner.current = {
        reservation: parked.reservation,
        generation: recoveryGeneration.current,
        editor,
      };
      pendingCanonicalRecovery.current = parked.snapshot;
      startRecoveryPoll(
        parked.snapshot.chatId,
        null,
        undefined,
        parked.reservation,
      );
    }
    if (isWorkflowSwitch) {
      setRecoveredPauseFrames([]);
      setQuestionInteractions([]);
      setQuestionCancelToken(null);
      setWorkflowCopilotChatId(null);
      workflowCopilotChatIdRef.current = null;
    }

    let isMounted = true;

    const fetchHistory = async () => {
      const loadSeq = beginHistoryLoad();
      setStartupFailed(false);
      repin();
      // A later navigation must not restore the old chat's auto-accept setting.
      const navEpochAtStart = chatNavEpochRef.current;
      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        const response =
          await readCredentialRecoveryHistory<WorkflowCopilotChatHistoryResponse>(
            client,
            workflowPermanentId,
            {
              params: {
                workflow_permanent_id: workflowPermanentId,
                ...(parked || stored
                  ? {
                      workflow_copilot_chat_id:
                        parked?.snapshot.chatId ?? stored?.chatId,
                    }
                  : {}),
              },
            },
            { retryTransientFailure: true },
          );

        if (!isMounted || chatNavEpochRef.current !== navEpochAtStart) return;

        applyHistoryResponse(response.data);
        adoptRecoveredTurns(response.data, response.hadCredentialRecoveryToken);
        historyLoadedForRef.current = workflowPermanentId;
        if (!deferredStartupClaim.current)
          setStartupReadyFor(workflowPermanentId);
      } catch (error) {
        if (isMounted) setStartupFailed(true);
        console.error("Failed to load chat history:", error);
      } finally {
        if (isMounted) {
          endHistoryLoad(loadSeq);
        }
      }
    };

    fetchHistory();

    return () => {
      isMounted = false;
    };
  }, [
    startupRetry,
    credentialGetter,
    initialAction?.nonce,
    adoptRecoveredTurns,
    repin,
    stopRecoveryPolls,
    startRecoveryPoll,
    updateQueuedPrompt,
    workflowPermanentId,
    applyHistoryResponse,
    beginHistoryLoad,
    endHistoryLoad,
    discardQueuedPrompt,
  ]);

  // Set by a block's "Generate" arm step so the next send scopes regeneration to that block.
  const blockBuildTargetLabelRef = useRef<string | null>(null);
  // Set by a product affordance so the next send posts that typed action instead of prose.
  const productActionRef = useRef<ArmedProductAction | null>(null);
  const echoSenderForArmedAction = (): WorkflowCopilotChatSender =>
    isProductAuthoredAction(productActionRef.current) ? "product" : "user";
  // True only while a block-build turn is actually in flight (not a turn it queued behind).
  const blockGenInFlightRef = useRef(false);

  // Disposal path that hands the queued text back to the composer as an
  // editable draft; Remove (keepText: false) and the drain effect's duplicate
  // drop are the paths that discard it. Reads the synchronous ref, not state, so
  // a stop can clear the queue before isLoading flips and the drain effect runs.
  const restoreQueuedPromptToComposer = useCallback(
    ({ keepText = true }: { keepText?: boolean } = {}) => {
      const queued = queuedPromptRef.current;
      if (!queued) {
        return;
      }

      // A product action's queued text is the server's receipt, not words the user wrote, so
      // handing it back as an editable draft would repost it as a user message.
      const wasProductAuthoredAction = isProductAuthoredAction(
        productActionRef.current,
      );

      updateQueuedPrompt(null);
      // Drop the queued block-build target and end-to-end action so neither leaks into the next
      // message. Deferring paths (queue_working / queue_live_browser) keep the action armed on
      // purpose; only abandoning the message disarms it.
      blockBuildTargetLabelRef.current = null;
      productActionRef.current = null;
      setMessages((prev) => prev.filter((message) => message.id !== queued.id));
      // Text half-typed in the composer was going to be added to the queued message, so it
      // follows the queued text rather than replacing it.
      if (keepText && !wasProductAuthoredAction) {
        setInputValue((current) => appendQueuedText(queued.content, current));
      }
      // The files belong to the queued message, so they return to the tray whether it is edited
      // or removed. Without this the resubmitted message silently goes out with no attachment.
      returnFilesToTray(queued.attachments ?? []);
      window.requestAnimationFrame(() => {
        textareaRef.current?.focus();
        adjustTextareaHeight();
      });
    },
    [adjustTextareaHeight, returnFilesToTray, updateQueuedPrompt],
  );

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
      const reservation = copilotReservation.current;
      if (
        !controllerAtCancel ||
        !reservation ||
        !isCopilotTurnCurrent(reservation)
      )
        return;

      const cancelToken = pendingCancelToken.current;
      pendingCancelToken.current = null;
      // After the token check, not before: a turn that already finished has no
      // token, and claiming "Stopping…" for a cancel that never goes out would
      // rely on the isLoading reset as its only way back.
      if (!cancelToken) {
        canonicalRecoveryAbort.current?.abort();
        return;
      }

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

      let cancelRequestStarted = false;
      let settled = false;
      const isCurrent = () =>
        !settled &&
        isCopilotTurnCurrent(reservation) &&
        streamingAbortController.current === controllerAtCancel &&
        !controllerAtCancel.signal.aborted;
      const abortUnconfirmed = (content: string) => {
        if (!isCurrent()) return;
        settled = true;
        if (cancelSafetyTimer.current !== null) {
          clearTimeout(cancelSafetyTimer.current);
          cancelSafetyTimer.current = null;
        }
        appendStopUnconfirmedNotice(content);
        controllerAtCancel.abort();
      };
      if (cancelSafetyTimer.current !== null) {
        clearTimeout(cancelSafetyTimer.current);
      }
      cancelSafetyTimer.current = setTimeout(
        () =>
          abortUnconfirmed(
            cancelRequestStarted
              ? STOP_UNCONFIRMED_NOTICE
              : STOP_NOT_SENT_NOTICE,
          ),
        15_000,
      );
      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        if (!isCurrent()) return;
        cancelRequestStarted = true;
        await client.post<void>(
          "/workflow/copilot/cancel",
          { cancel_token: cancelToken, source } as WorkflowCopilotCancelRequest,
          { timeout: 15_000, signal: controllerAtCancel.signal },
        );
      } catch (error) {
        if (!isCurrent()) return;
        console.warn("Workflow copilot cancel POST failed", error);
        abortUnconfirmed(STOP_NOT_SENT_NOTICE);
      }
    },
    [credentialGetter, restoreQueuedPromptToComposer, isCopilotTurnCurrent],
  );

  // Stream cleanup ends the Stop spinner; recovery separately confirms whether
  // the backend cancelled or committed the turn.
  useEffect(() => {
    const resume = () => {
      if (
        !composerMountedRef.current ||
        !workflowPermanentId ||
        pendingCanonicalRecovery.current
      )
        return;
      const parked = canonicalRecoveriesByWorkflow.get(workflowPermanentId);
      if (!parked) return;
      const state = useWorkflowYamlEditorStore.getState();
      if (
        state.commitInProgress ||
        state.copilotAcceptance ||
        state.authoringInProgress
      )
        return;
      const reservation = reserveCopilotTurn();
      if (!reservation) return;
      canonicalRecoveriesByWorkflow.delete(workflowPermanentId);
      createCanonicalRecovery(reservation, {
        ...parked,
        hadLocalEdits:
          parked.hadLocalEdits ||
          useWorkflowHasChangesStore.getState().hasChanges ||
          (state.active &&
            (state.stale || state.draft !== state.entrySnapshot)),
      });
      startRecoveryPoll(
        parked.poll?.chatId ?? null,
        parked.poll?.turnId ?? null,
        parked.poll?.requestId ?? crypto.randomUUID(),
        reservation,
        parked.poll?.ownsTurn,
        parked.poll?.recordingRefinementMessageId,
      );
    };
    resume();
    return useWorkflowYamlEditorStore.subscribe(resume);
  }, [
    workflowPermanentId,
    reserveCopilotTurn,
    createCanonicalRecovery,
    startRecoveryPoll,
  ]);

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
      if (
        !workflowCopilotChatId ||
        submittingQuestion.current ||
        acceptUnresolved
      )
        return false;
      if (
        !streamingAbortController.current &&
        !startRecoveryPoll(
          workflowCopilotChatId,
          interaction.turn_id,
          undefined,
          undefined,
          true,
        )
      )
        return false;
      const reservation = copilotReservation.current;
      if (!reservation || !isCopilotTurnCurrent(reservation)) return false;
      submittingQuestion.current = true;
      setIsSubmittingQuestion(true);
      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        if (!isCopilotTurnCurrent(reservation)) return false;
        const accepted = await client.post<QuestionInteraction>(
          "/workflow/copilot/question-response",
          {
            workflow_copilot_chat_id: workflowCopilotChatId,
            interaction_id: interaction.interaction_id,
            ...response,
          },
        );
        if (
          !isCopilotTurnCurrent(reservation) ||
          workflowCopilotChatIdRef.current !== workflowCopilotChatId
        )
          return true;
        setQuestionInteractions((current) =>
          current.map((item) =>
            item.interaction_id === interaction.interaction_id
              ? accepted.data
              : item,
          ),
        );
        setIsLoading(true);
        if (!streamingAbortController.current) {
          startRecoveryPoll(
            workflowCopilotChatId,
            interaction.turn_id,
            undefined,
            undefined,
            true,
          );
        }
        return true;
      } catch {
        if (!isCopilotTurnCurrent(reservation)) return false;
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
        if (composerMountedRef.current) setIsSubmittingQuestion(false);
      }
    },
    [
      acceptUnresolved,
      credentialGetter,
      isCopilotTurnCurrent,
      workflowCopilotChatId,
      loadChatInPlace,
      startRecoveryPoll,
    ],
  );

  const hasPendingQuestion = questionInteractions.some(
    (item) => item.status === "pending",
  );
  const uploadDroppedAttachments = useCallback(
    (files: FileList) => {
      const droppedFiles = Array.from(files);
      const supportedFiles = droppedFiles.filter(isSupportedAttachment);
      const unsupportedFiles = droppedFiles.filter(
        (file) => !isSupportedAttachment(file),
      );
      const oversizedFiles = supportedFiles.filter(
        (file) => file.size > attachmentSizeLimit(file),
      );
      const uploadableFiles = supportedFiles.filter(
        (file) => file.size <= attachmentSizeLimit(file),
      );
      if (unsupportedFiles.length > 0) {
        toast({
          variant: "destructive",
          title: "Unsupported file type",
          description: `${unsupportedFiles.map((file) => file.name).join(", ")} cannot be attached.`,
        });
      }

      const uploadingCount = pendingAttachmentsRef.current.filter(
        (item) => item.status === "uploading",
      ).length;
      const availableSlots = Math.max(
        0,
        ATTACHMENT_COUNT_LIMIT - attachmentsRef.current.length - uploadingCount,
      );
      if (uploadableFiles.length > availableSlots) {
        toast({
          variant: "destructive",
          title: "Too many files",
          description: `A message can carry up to ${ATTACHMENT_COUNT_LIMIT} files.`,
        });
      }
      oversizedFiles.forEach((file) => void uploadAttachment(file));
      uploadableFiles
        .slice(0, availableSlots)
        .forEach((file) => void uploadAttachment(file));
    },
    [uploadAttachment],
  );

  const handleComposerDragEnter = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      if (!hasFileDragPayload(event.dataTransfer)) return;
      event.preventDefault();
      fileDragDepthRef.current += 1;
      if (!hasPendingQuestion) setIsFileDragging(true);
    },
    [hasPendingQuestion],
  );

  const handleComposerDragOver = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      if (!hasFileDragPayload(event.dataTransfer)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = hasPendingQuestion ? "none" : "copy";
    },
    [hasPendingQuestion],
  );

  const handleComposerDragLeave = useCallback(() => {
    fileDragDepthRef.current = Math.max(0, fileDragDepthRef.current - 1);
    if (fileDragDepthRef.current === 0) setIsFileDragging(false);
  }, []);

  const handleComposerDrop = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      if (!hasFileDragPayload(event.dataTransfer)) return;
      event.preventDefault();
      fileDragDepthRef.current = 0;
      setIsFileDragging(false);
      if (hasPendingQuestion) {
        toast({
          title: "Answer the pending question first",
          description: "You can attach files to your next message afterward.",
        });
        return;
      }
      uploadDroppedAttachments(event.dataTransfer.files);
    },
    [hasPendingQuestion, uploadDroppedAttachments],
  );
  useEffect(() => {
    if (!hasPendingQuestion) return;
    fileDragDepthRef.current = 0;
    setIsFileDragging(false);
  }, [hasPendingQuestion]);
  useEffect(() => {
    if (!hasPendingQuestion || !workflowCopilotChatId) return;
    let disposed = false;
    const generation = recoveryGeneration.current;
    const editor = useWorkflowYamlEditorStore.getState().editorOwner;
    const refresh = async () => {
      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        const { data } =
          await readCredentialRecoveryHistory<WorkflowCopilotChatHistoryResponse>(
            client,
            workflowPermanentId,
            {
              params: { workflow_copilot_chat_id: workflowCopilotChatId },
            },
          );
        if (
          disposed ||
          !composerMountedRef.current ||
          recoveryGeneration.current !== generation ||
          useWorkflowYamlEditorStore.getState().editorOwner !== editor ||
          (editor && !editor.active)
        )
          return;
        rememberRecoveryCancelTokens(data);
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
            startRecoveryPoll(
              workflowCopilotChatId,
              resolved.turn_id,
              undefined,
              undefined,
              true,
            );
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
    rememberRecoveryCancelTokens,
    workflowCopilotChatId,
    workflowPermanentId,
    credentialGetter,
    startRecoveryPoll,
  ]);

  const handleSend = useCallback(
    async (
      messageOverride?: string,
      options: SendOptions = {},
    ): Promise<boolean> => {
      const generation = recoveryGeneration.current;
      if (
        !options.queuedMessageId &&
        useWorkflowYamlEditorStore.getState().commitInProgress &&
        refuseMutationDuringYamlCommit()
      )
        return false;
      if (authoringInProgress) return false;
      // A turn started now could stage a proposal the pending Accept then clears, and the
      // setGateFailure below would drop a hold whose write is still unaccounted for.
      // Callers that consume their input first (queue, auto-send, Generate) wait for it instead.
      if (acceptUnresolved) {
        return false;
      }
      const candidate = messageOverride ?? inputValue;
      const pendingQuestion = questionInteractions.find(
        (item) => item.status === "pending",
      );
      if (pendingQuestion && candidate !== "") {
        if (await handleQuestionAnswer(pendingQuestion, { text: candidate }))
          setInputValue("");
        return false;
      }
      const isDrain = Boolean(options.queuedMessageId);
      // The tray belongs to the message the user is composing, so only that send may spend it —
      // whether it sends now, queues, or replaces a queued message. A drain carries the queued
      // message's own files, and a programmatic send (Test end-to-end, an account choice, a block
      // regeneration) is not the user's message at all; either taking the tray would attach the
      // user's file to something they did not attach it to.
      const composerSend =
        messageOverride === undefined && options.attachments === undefined;
      let sentAttachments =
        options.attachments ?? (composerSend ? attachments : []);
      // Only failed chips go: an upload can start while dictation finalizes, and that file is for the next message.
      const clearSentTray = () => {
        setAttachments([]);
        setPendingAttachments((prev) =>
          prev.filter((item) => item.status !== "error"),
        );
      };
      if (composerSend && consumedComposerTextRef.current === candidate) {
        return false;
      }
      const incomingOrigin = (): QueuedPromptOrigin =>
        isProductAuthoredAction(productActionRef.current)
          ? "product"
          : options.selectedConnectedAccountId !== undefined ||
              options.idempotencyKey !== undefined ||
              blockBuildTargetLabelRef.current !== null ||
              productActionRef.current !== null
            ? "programmatic"
            : "typed";
      let action = resolveSendAction({
        inFlight: inFlightRef.current || recoveredTurnOwnerRef.current !== null,
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
        if (
          composerSend &&
          (attachments.length > 0 ||
            pendingAttachments.some((item) => item.status === "uploading"))
        ) {
          toast({
            title: "Add a message",
            description: "Tell Copilot what to do with the attached file.",
          });
        }
        return false;
      }
      if (recordingFocusOpen) {
        setRecordingFocusOpen(false);
      }
      if (!workflowPermanentId) {
        productActionRef.current = null;
        toast({
          title: "Missing agent",
          description: "Agent permanent ID is required to chat.",
          variant: "destructive",
        });
        return false;
      }

      if (action === "send" && refuseMutationDuringYamlCommit()) {
        if (!options.queuedMessageId) return false;
        action = "queue_working";
      }

      // Only a composer-sourced send may be refused. A drain — with or without files of its own —
      // has no stake in an upload meant for the next message, and its queued prompt is already
      // cleared, so refusing it would drop that message with nothing left to re-drain.
      if (
        composerSend &&
        pendingAttachments.some((item) => item.status === "uploading")
      ) {
        toast({
          title: "Upload in progress",
          description:
            "Wait for the attachment to finish uploading, then send.",
        });
        return false;
      }
      // A queued message's files return to the tray on edit or discard, so the tray can pass the
      // per-message limit that attaching enforces; refuse before anything leaves it.
      if (composerSend && attachments.length > ATTACHMENT_COUNT_LIMIT) {
        toast({
          title: "Too many files",
          description: `A message can carry up to ${ATTACHMENT_COUNT_LIMIT} files. Remove ${attachments.length - ATTACHMENT_COUNT_LIMIT} to send.`,
        });
        return false;
      }
      const withQueuedFiles = (tray: CopilotAttachedFile[]) => {
        const queuedFiles = queuedPromptRef.current?.attachments ?? [];
        const queuedFileIds = new Set(queuedFiles.map((item) => item.file_id));
        return [
          ...queuedFiles,
          ...tray.filter((item) => !queuedFileIds.has(item.file_id)),
        ];
      };
      // Refuse before dictation is finalized below, which consumes the recording.
      if (action === "append_queued" && composerSend) {
        const combinedCount = withQueuedFiles(attachments).length;
        if (combinedCount > ATTACHMENT_COUNT_LIMIT) {
          toast({
            title: "Too many files",
            description: `A message can carry up to ${ATTACHMENT_COUNT_LIMIT} files. Remove ${combinedCount - ATTACHMENT_COUNT_LIMIT} to add these to the queued message.`,
          });
          return false;
        }
      }

      let messageAudioBlob = options.audioBlob ?? null;
      if (!messageAudioBlob && messageOverride === undefined) {
        if (isSpeechListening) {
          messageAudioBlob = await stopSpeech();
        }
        messageAudioBlob = messageAudioBlob ?? takeSpeechAudioBlob();
      }
      // A second Enter pressed while dictation finalized may have queued this text already.
      if (composerSend && consumedComposerTextRef.current === candidate) {
        return false;
      }
      if (composerSend) {
        // The tray stays interactive while dictation finalizes, so a file removed then must not go.
        sentAttachments = attachmentsRef.current;
      }
      // A delete is already on the wire for these, so no send may name them — a drain carries the
      // queued message's own files and would otherwise skip this.
      if (
        sentAttachments.some((attached) =>
          reclaimingFiles.current.has(attached.file_id),
        )
      ) {
        sentAttachments = sentAttachments.filter(
          (attached) => !reclaimingFiles.current.has(attached.file_id),
        );
        toast({
          title: "Attachment removed",
          description:
            "A file attached to this message was deleted, so it was not sent.",
        });
      }

      if (action === "append_queued") {
        const queued = queuedPromptRef.current;
        if (!queued) {
          return false;
        }
        const combinedAttachments = composerSend
          ? withQueuedFiles(sentAttachments)
          : (queued.attachments ?? []);
        if (combinedAttachments.length > ATTACHMENT_COUNT_LIMIT) {
          toast({
            title: "Too many files",
            description: `A message can carry up to ${ATTACHMENT_COUNT_LIMIT} files. Remove ${combinedAttachments.length - ATTACHMENT_COUNT_LIMIT} to add these to the queued message.`,
          });
          return false;
        }
        // Only typed text is added to typed text; anything programmatic replaces as it always has.
        const addsToUserText = composerSend && queued.origin === "typed";
        // Until cleared below, the armed refs are the queued message's, not a composer send's.
        const origin = composerSend ? "typed" : incomingOrigin();
        // New text: the block-build scope and the end-to-end action belonged to the message as it
        // was queued. Carrying the action over would run the whole workflow for real on text the
        // user added to say something else.
        blockBuildTargetLabelRef.current = null;
        productActionRef.current = null;
        const content = addsToUserText
          ? appendQueuedText(queued.content, candidate)
          : candidate;
        updateQueuedPrompt({
          ...queued,
          origin,
          content,
          audioBlob: addsToUserText
            ? (messageAudioBlob ?? queued.audioBlob)
            : messageAudioBlob,
          idempotencyKey: options.idempotencyKey,
          selectedConnectedAccountId: options.selectedConnectedAccountId,
          attachments: combinedAttachments,
          recording: options.recording ?? snapshotRecording(),
        });
        if (composerSend) {
          clearSentTray();
          consumedComposerTextRef.current = candidate;
        }
        // Only a Home handoff has a bubble while queued; keep it in step with what will be sent.
        setMessages((prev) =>
          prev.map((message) =>
            message.id === queued.id
              ? {
                  ...message,
                  sender: "user",
                  content,
                  attachedFiles:
                    combinedAttachments.length > 0
                      ? combinedAttachments
                      : undefined,
                }
              : message,
          ),
        );
        if (messageOverride === undefined) {
          setInputValue("");
        }
        return true;
      }

      if (action === "queue_working" || action === "queue_live_browser") {
        const reason: QueuedPromptReason =
          action === "queue_working" ? "working" : "live_browser";
        const queuedId =
          options.queuedMessageId ??
          options.optimisticMessageId ??
          crypto.randomUUID();
        updateQueuedPrompt({
          id: queuedId,
          origin: incomingOrigin(),
          content: candidate,
          reason,
          audioBlob: messageAudioBlob,
          idempotencyKey: options.idempotencyKey,
          selectedConnectedAccountId: options.selectedConnectedAccountId,
          attachments: sentAttachments,
          recording: options.recording ?? snapshotRecording(),
        });
        if (composerSend) {
          clearSentTray();
        }
        if (composerSend) consumedComposerTextRef.current = candidate;
        // The queued strip above the composer stands in for the message until it is delivered;
        // the send path adds its bubble then.
        if (messageOverride === undefined) {
          setInputValue("");
        }
        return true;
      }

      // Effect-driven sends must leave React's commit before flushing editor state.
      if (options.deferReservation) await Promise.resolve();
      if (
        !composerMountedRef.current ||
        generation !== recoveryGeneration.current
      )
        return false;

      const reservation = reserveCopilotTurn("send");
      if (!reservation) return false;
      copilotReservation.current = reservation;
      const userMessageId =
        options.queuedMessageId ??
        options.optimisticMessageId ??
        Date.now().toString();
      const sendOwnsTray = composerSend;
      const recordingRefinementAction =
        productActionRef.current?.action === "refine_recording"
          ? productActionRef.current
          : null;
      const recordingRefinement = recordingRefinementAction
        ? {
            actionCount:
              useRecordingRefinementEvidenceStore
                .getState()
                .peek(recordingRefinementAction.nonce)?.actions.length ?? 0,
            startedAtMs: Date.now(),
            status: "working" as const,
          }
        : undefined;
      const userMessage: ChatMessage = {
        id: userMessageId,
        sender: echoSenderForArmedAction(),
        content: candidate,
        kind: recordingRefinement ? "recording_refinement" : undefined,
        recordingRefinement,
        attachedFiles: sentAttachments.length > 0 ? sentAttachments : undefined,
      };
      if (sendOwnsTray) {
        clearSentTray();
      }
      const registeredAttachments = sentAttachments;
      unpostedSendsRef.current.add(registeredAttachments);

      const cancelToken = crypto.randomUUID();
      pendingCancelToken.current = cancelToken;
      try {
        if (workflowPermanentId)
          sessionStorage.setItem(
            requestTokenKey(workflowPermanentId),
            cancelToken,
          );
      } catch {
        // Recovery in this mounted editor still retains the request token.
      }
      buildFollowEngaged.current = true;
      lastFollowedLabelRef.current = null;

      pendingMessageId.current = userMessageId;
      // A queued message gets its bubble here, at delivery. Only a Home handoff already has one.
      setMessages((prev) =>
        prev.some((message) => message.id === userMessageId)
          ? prev.map((message) =>
              message.id === userMessageId
                ? {
                    ...message,
                    kind: recordingRefinement
                      ? "recording_refinement"
                      : message.kind,
                    recordingRefinement:
                      recordingRefinement ?? message.recordingRefinement,
                    // Also when the filtered list is empty: the bubble must not keep showing
                    // files the request did not carry.
                    attachedFiles:
                      sentAttachments.length > 0 ? sentAttachments : undefined,
                  }
                : message,
            )
          : [...prev, userMessage],
      );
      const messageContent = candidate;
      let chatIdForRequest = workflowCopilotChatId;
      if (messageOverride === undefined && !options.queuedMessageId) {
        setInputValue("");
      }
      if (composerSend) consumedComposerTextRef.current = candidate;
      setIsLoading(true);
      inFlightRef.current = true;
      sendEpoch.current += 1;
      sendEpochRef.current += 1;
      setGateFailure(null);
      // Stamped here, before the awaits below consume messageAudioBlob
      // and blockBuildTargetLabelRef.
      lastTurnRef.current = {
        content: candidate,
        workflowPermanentId,
        hadAudio: messageAudioBlob !== null,
        hadBlockTarget: blockBuildTargetLabelRef.current !== null,
        browserSessionId: liveBrowserSessionId ?? null,
        attachmentIds: sentAttachments.map((attached) => attached.file_id),
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
      const sendGeneration = recoveryGeneration.current;
      const sendPresentationGeneration = chatPresentationGeneration.current;
      let streamTurnId: string | null = null;
      let requestStarted = false;
      let definitiveRejection = false;
      let submittedSnapshot: TurnSnapshot | null = null;
      // No message holds these files unless the server saved the turn before it was cut off, so they
      // come back to the tray. Once the request went out they are kept if removed; before that, a
      // removal still deletes them.
      const returnPossiblySavedFiles = () => {
        if (streamTurnId !== null || !composerMountedRef.current) {
          return;
        }
        if (requestStarted) {
          for (const attached of sentAttachments) {
            postedFileIds.current.add(attached.file_id);
          }
        }
        returnFilesToTray(sentAttachments);
      };
      let streamChatId: string | null = null;
      let sawTerminalFrame = false;
      let canonicalAtSubmit: WorkflowApiResponse | undefined;
      let submittedSettings: WorkflowSettings | undefined;
      let terminalRecovery: Promise<boolean | undefined> | undefined;
      let adoptRecoveryTurn: ReturnType<typeof startRecoveryPoll>;
      let sendFinished = false;
      let sawCredentialPause = false;
      let unconfirmedRetained = false;
      const captureRecovery = (): CanonicalRecovery | null => {
        if (
          !requestStarted ||
          definitiveRejection ||
          sawTerminalFrame ||
          !canonicalAtSubmit
        )
          return null;
        const yaml = useWorkflowYamlEditorStore.getState();
        return {
          workflowPermanentId,
          baseline: canonicalAtSubmit,
          awaitingTurnId: streamTurnId ?? undefined,
          terminalConfirmed: false,
          rollback: submittedSnapshot ?? undefined,
          preservedSettings: submittedSettings,
          waitingForUnlock: false,
          yaml: yaml.active
            ? { draft: yaml.draft, entrySnapshot: yaml.entrySnapshot }
            : null,
          poll: {
            chatId: streamChatId ?? chatIdForRequest,
            turnId: streamTurnId,
            requestId: cancelToken,
            deadline: Date.now() + RECOVERY_POLL_BUDGET_MS,
            ownsTurn: sawCredentialPause,
            recordingRefinementMessageId: recordingRefinement
              ? userMessageId
              : undefined,
          },
        };
      };
      captureLiveTurnRecovery.current = captureRecovery;
      const rollbackOrReconcile = (
        entry: TurnSnapshot | null,
        turnId: string | null,
      ) => {
        if (
          !entry ||
          (!entry.hadStagedDraft &&
            !entry.titlePersisted &&
            !entry.workflowPersisted)
        )
          return;
        const pending = createCanonicalRecovery(reservation, {
          baseline: entry.workflowPersisted ? undefined : canonicalAtSubmit,
          awaitingTurnId: turnId ?? undefined,
          terminalConfirmed: entry.workflowPersisted || undefined,
          rollback: entry,
          restoreRollback: true,
          preservedSettings: submittedSettings ?? entry.settings,
          waitingForUnlock: false,
        });
        if (!pending) return;
        terminalRecovery = reconcileCanonicalWorkflow(
          reservation,
          abortController.signal,
        );
      };
      const retainUnconfirmedOutcome = () => {
        if (
          unconfirmedRetained ||
          !requestStarted ||
          definitiveRejection ||
          sawTerminalFrame ||
          !canonicalAtSubmit ||
          recoveryGeneration.current !== sendGeneration ||
          pendingCanonicalRecovery.current
        )
          return;
        if (
          !createCanonicalRecovery(reservation, {
            baseline: canonicalAtSubmit,
            awaitingTurnId: streamTurnId ?? undefined,
            terminalConfirmed: false,
            rollback: submittedSnapshot ?? undefined,
            preservedSettings: submittedSettings,
            waitingForUnlock: false,
          })
        )
          return;
        unconfirmedRetained = true;
        if (!recordingRefinement)
          adoptRecoveryTurn = startRecoveryPoll(
            streamChatId ?? workflowCopilotChatIdRef.current,
            streamTurnId,
            cancelToken,
            reservation,
            sawCredentialPause,
            undefined,
            sendPresentationGeneration,
          );
        if (!abortController.signal.aborted) {
          terminalRecovery = reconcileCanonicalWorkflow(
            reservation,
            abortController.signal,
          );
        }
      };
      const shouldArmRecovery = () =>
        isCopilotTurnCurrent(reservation) &&
        requestStarted &&
        !definitiveRejection &&
        (streamTurnId !== null || pendingCanonicalRecovery.current !== null) &&
        recoveryGeneration.current === sendGeneration &&
        (Boolean(pendingCanonicalRecovery.current) || !sawTerminalFrame);
      // An aborted upload cannot change the workflow before chat-post starts,
      // so a new chat need not wait for that upload to release the editor.
      const releaseUnsentReservation = () => {
        if (requestStarted) return;
        finishCopilotAcceptance(reservation);
        if (copilotReservation.current === reservation)
          copilotReservation.current = null;
      };
      abortController.signal.addEventListener(
        "abort",
        releaseUnsentReservation,
        { once: true },
      );
      let recoveryNoticeId: string | null = null;
      const finishRecordingRefinement = (
        status: Exclude<RecordingRefinementStatus, "working">,
      ) => {
        setMessages((current) =>
          current.map((message) =>
            message.id === userMessageId && message.recordingRefinement
              ? {
                  ...message,
                  recordingRefinement: {
                    ...message.recordingRefinement,
                    status,
                  },
                }
              : message,
          ),
        );
      };
      const recoverPersistedRecordingTurn = async (
        signal = abortController.signal,
      ): Promise<boolean> => {
        if (!recordingRefinement || !requestStarted || streamTurnId !== null) {
          return false;
        }
        try {
          const client = await getClient(credentialGetter, "sans-api-v1");
          const response = await client.get<WorkflowCopilotChatHistoryResponse>(
            "/workflow/copilot/chat-history",
            {
              params: chatIdForRequest
                ? {
                    workflow_copilot_chat_id: chatIdForRequest,
                    request_cancel_token: cancelToken,
                  }
                : {
                    workflow_permanent_id: workflowPermanentId,
                    request_cancel_token: cancelToken,
                  },
              signal,
            },
          );
          if (!isCopilotTurnCurrent(reservation) || signal?.aborted)
            return false;
          const earliestMatchingCreatedAt =
            recordingRefinement.startedAtMs - 5 * 60 * 1000;
          const opener = [...response.data.chat_history]
            .reverse()
            .find(
              (message) =>
                message.sender === "product" &&
                message.turn_id &&
                message.content === messageContent &&
                parseServerStamp(message.created_at) >=
                  earliestMatchingCreatedAt,
            );
          const recoveredTurnId =
            response.data.request_turn_id === undefined
              ? opener?.turn_id
              : (response.data.request_turn_id ?? undefined);
          if (!recoveredTurnId) {
            return false;
          }
          streamTurnId = recoveredTurnId;
          streamChatId = response.data.workflow_copilot_chat_id;
          if (recordingRefinementAction) {
            useRecordingRefinementEvidenceStore
              .getState()
              .take(recordingRefinementAction.nonce);
            onInitialMessageConsumedRef.current?.();
          }
          if (
            chatPresentationGeneration.current === sendPresentationGeneration
          ) {
            setWorkflowCopilotChatId(streamChatId);
            workflowCopilotChatIdRef.current = streamChatId;
          }
          setMessages((current) =>
            current.map((message) =>
              message.id === userMessageId && message.recordingRefinement
                ? {
                    ...message,
                    recordingRefinement: {
                      ...message.recordingRefinement,
                      turnId: recoveredTurnId,
                    },
                  }
                : message,
            ),
          );
          return true;
        } catch (historyError) {
          console.warn(
            "Failed to recover recording refinement after losing turn_start:",
            historyError,
          );
          return false;
        }
      };
      const startPersistedRecordingTurnLookup = () => {
        if (!isCopilotTurnCurrent(reservation)) return;
        const recoveryKey = `recording-request:${userMessageId}`;
        const generation = sendGeneration;
        const deadline = Date.now() + RECOVERY_POLL_BUDGET_MS;
        let step = 0;
        let timer: ReturnType<typeof setTimeout> | null = null;
        let deadlineTimer: ReturnType<typeof setTimeout> | null = null;
        let inFlight: AbortController | null = null;
        let stopped = false;

        const finish = () => {
          stopped = true;
          if (timer !== null) {
            clearTimeout(timer);
            timer = null;
          }
          inFlight?.abort();
          inFlight = null;
          if (deadlineTimer !== null) {
            clearTimeout(deadlineTimer);
            deadlineTimer = null;
          }
          if (recoveryPolls.current.get(recoveryKey)?.stop === finish) {
            recoveryPolls.current.delete(recoveryKey);
          }
        };
        const giveUp = () => {
          finish();
          if (!isCopilotTurnCurrent(reservation)) return;
          finishRecordingRefinement("failed");
          if (recoveryNoticeId) {
            setMessages((current) =>
              current.map((message) =>
                message.id === recoveryNoticeId
                  ? { ...message, content: SEND_FAILED_MESSAGE }
                  : message,
              ),
            );
          }
        };
        const schedule = () => {
          if (stopped) return;
          if (Date.now() >= deadline) {
            giveUp();
            return;
          }
          const delay =
            RECOVERY_POLL_DELAYS_MS[step] ?? RECOVERY_POLL_STEADY_MS;
          step += 1;
          timer = setTimeout(() => {
            timer = null;
            void tick();
          }, delay);
        };
        const tick = async () => {
          if (
            stopped ||
            !isCopilotTurnCurrent(reservation) ||
            recoveryGeneration.current !== generation
          ) {
            finish();
            return;
          }
          const controller = new AbortController();
          inFlight = controller;
          const recovered = await recoverPersistedRecordingTurn(
            controller.signal,
          );
          if (inFlight === controller) {
            inFlight = null;
          }
          if (stopped || !isCopilotTurnCurrent(reservation)) return;
          if (recovered) {
            finish();
            if (streamTurnId && shouldArmRecovery()) {
              if (recoveryNoticeId) {
                setMessages((current) =>
                  current.map((message) =>
                    message.id === recoveryNoticeId
                      ? {
                          ...message,
                          recoveryTurnId: streamTurnId ?? undefined,
                        }
                      : message,
                  ),
                );
              }
              const recoveredChatId =
                streamChatId ?? workflowCopilotChatIdRef.current;
              if (
                sendFinished &&
                !adoptRecoveryTurn?.(recoveredChatId, streamTurnId)
              ) {
                adoptRecoveryTurn = startRecoveryPoll(
                  recoveredChatId,
                  streamTurnId,
                  cancelToken,
                  undefined,
                  sawCredentialPause,
                  userMessageId,
                  sendPresentationGeneration,
                );
              }
            }
            return;
          }
          schedule();
        };

        recoveryPolls.current.get(recoveryKey)?.stop();
        recoveryPolls.current.set(recoveryKey, {
          stop: finish,
          isReserved: () => copilotReservation.current === reservation,
        });
        deadlineTimer = setTimeout(giveUp, RECOVERY_POLL_BUDGET_MS);
        void tick();
      };

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
        const saveData = saveDataGetter.current();
        canonicalAtSubmit = saveData?.workflow;
        const workflowId = saveData?.workflow.workflow_id;
        let workflowYaml = "";
        let audioArtifactId: string | null = null;

        if (!workflowId) {
          productActionRef.current = null;
          // Nothing was sent, so whichever message these files came with — the composer's or a
          // drained queued one, whose queued prompt is already gone — they return to the tray.
          returnFilesToTray(sentAttachments);
          toast({
            title: "Missing agent",
            description: "Agent ID is required to chat.",
            variant: "destructive",
          });
          return false;
        }

        if (saveData) {
          const { document } = buildWorkflowCopilotContext({
            ...saveData,
            definitionVersion: saveData.workflowDefinitionVersion,
          });
          workflowYaml = convertToYAML(document);
          submittedSettings = structuredClone(saveData.settings);
          pendingSubmitSettings.current = submittedSettings;

          submittedSnapshot = {
            snapshot: pendingSubmitSnapshot.current,
            settings: submittedSettings,
            hadStagedDraft: false,
            workflowPersisted: false,
            titlePersisted: false,
          };
        }

        if (messageAudioBlob) {
          try {
            const uploadResponse = await uploadDictationAudio(
              messageAudioBlob,
              reservation,
            );
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
          if (response.work_plan) {
            setWorkPlan(response.work_plan);
          }
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
            cancelInFlightController.current === abortController &&
            response.workflow_applied !== true;
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
          const refinementProducedWorkflow = Boolean(
            response.updated_workflow &&
            response.proposal_disposition !== "no_proposal",
          );
          finishRecordingRefinement(
            response.cancelled || userCancelledThisTurn
              ? "cancelled"
              : frozenNarrative?.terminal === "error" ||
                  runFailed ||
                  !refinementProducedWorkflow
                ? "failed"
                : "complete",
          );
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
            ? (turnSnapshots.current.get(responseTurnId) ?? submittedSnapshot)
            : submittedSnapshot;
          const preservedSettings =
            submittedSettings ?? responseEntry?.settings;
          if (response.updated_workflow) {
            useWorkflowTitleStore
              .getState()
              .trackCopilotMetadata(
                workflowPermanentId,
                `${response.workflow_copilot_chat_id}:${proposalTokenOf(response.proposed_workflow_metadata ?? null) ?? "legacy"}`,
              );
            proposalPreservedSettings.current = {
              workflow: response.updated_workflow,
              settings: preservedSettings,
            };
          }
          if (response.workflow_applied === true) {
            onWorkflowPersisted?.(workflowPermanentId);
            if (responseEntry) responseEntry.workflowPersisted = true;
            // This turn's auto-commit already moved canonical past any earlier
            // bypassed proposal — drop the stale handle so its gate cannot
            // reapply an outdated draft over what was just committed.
            setProposedWorkflow(null);
            setPendingProposalMetadata(null);
            setPendingProposalRun(null);
            setPendingProposalTurnId(null);
            const autoApplied =
              response.updated_workflow &&
              shouldAutoApplyWorkflowResponse(response) &&
              applyWorkflowUpdate(
                response.updated_workflow,
                { persisted: true, applied: true, fresh: true },
                reservation,
                preservedSettings,
              );
            if (autoApplied) {
              if (responseEntry) responseEntry.snapshot = null;
            } else {
              const pending = createCanonicalRecovery(reservation, {
                preservedSettings,
                baseline: undefined,
                rollback: responseEntry ?? undefined,
                terminalConfirmed: true,
                waitingForUnlock: true,
              });
              if (!pending) return;
              const chatId = response.workflow_copilot_chat_id;
              if (response.updated_workflow && chatId) {
                const snapshot: CopilotAcceptSnapshot & CanonicalRecovery = {
                  ...pending,
                  chatId,
                  acceptChatId: chatId,
                  acceptAttempt: null,
                  terminalConfirmed: true,
                  gateAttempt: {
                    alwaysAccept: false,
                    token: null,
                    wroteNothing: false,
                  },
                  savedWorkflow: response.updated_workflow,
                  savedOwnerTurnId: responseTurnId,
                };
                pendingCanonicalRecovery.current = snapshot;
                useWorkflowYamlEditorStore.setState((state) => ({
                  pendingAccepts: {
                    ...state.pendingAccepts,
                    [workflowPermanentId]: { reservation, snapshot },
                  },
                }));
                setProposedWorkflow(null);
                setPendingProposalMetadata(null);
                setPendingProposalRun(null);
                setPendingProposalTurnId(null);
                setGateFailure({
                  kind: "saved",
                  savedWorkflow: response.updated_workflow,
                  ownerTurnId: responseTurnId,
                  alwaysAccept: false,
                  token: null,
                  wroteNothing: false,
                });
              }
            }
          } else if (response.updated_workflow) {
            setProposedWorkflow(response.updated_workflow);
            setPendingProposalMetadata(
              response.proposed_workflow_metadata ?? null,
            );
            setPendingProposalRun(response.proposed_workflow_run ?? null);
            setPendingProposalTurnId(responseTurnId);
            if (
              !response.proposed_workflow_run &&
              response.proposed_workflow_metadata?.workflow_run_id
            ) {
              void backfillProposalRunFacts(
                response.proposed_workflow_metadata.owner_turn_id ?? null,
                response.workflow_copilot_chat_id,
                response.proposed_workflow_metadata.workflow_run_id,
              );
            }
          } else if (
            (response.cancelled || frozenNarrative?.terminal === "error") &&
            responseEntry &&
            (responseEntry.hadStagedDraft || responseEntry.titlePersisted)
          ) {
            rollbackOrReconcile(responseEntry, responseTurnId);
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
            setPendingProposalMetadata(
              response.updated_workflow
                ? (response.proposed_workflow_metadata ?? null)
                : null,
            );
            setPendingProposalRun(null);
            setPendingProposalTurnId(null);
          }
        };

        const handleError = (
          payload: WorkflowCopilotStreamErrorUpdate,
          errorNarrative?: TurnNarrativeState,
        ) => {
          finishRecordingRefinement("failed");
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
          // Errors on no-draft turns leave the canvas alone.
          const errorTurnId = payload.turn_id ?? latestTurnId.current ?? null;
          const errorEntry = errorTurnId
            ? (turnSnapshots.current.get(errorTurnId) ?? submittedSnapshot)
            : submittedSnapshot;
          if (errorEntry?.hadStagedDraft && errorEntry?.snapshot) {
            rollbackOrReconcile(errorEntry, errorTurnId);
            setProposedWorkflow(null);
            setPendingProposalMetadata(null);
            setPendingProposalRun(null);
            setPendingProposalTurnId(null);
          }
        };

        // Read before the awaits below, and unconditionally: this send owns the action, so a
        // throw on the way out must not leave it armed for whatever the user types next.
        const productAction = productActionRef.current;
        productActionRef.current = null;
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
        }
        // Unmount cleanup has already deleted this send's files. Any other abort (New chat, a chat
        // switch) leaves them unposted with no message holding them, so they go back to the tray.
        if (
          !isCopilotTurnCurrent(reservation) ||
          abortController.signal.aborted
        ) {
          if (isCopilotOwnerCurrent(reservation)) {
            returnFilesToTray(sentAttachments);
            if (abortController.signal.aborted) {
              finishRecordingRefinement("cancelled");
            }
          }
          return false;
        }
        unpostedSendsRef.current.delete(registeredAttachments);
        // Leaving the page during the awaits above can have deleted these ids, so the last word on
        // what this request carries is taken here rather than before them.
        const reclaimedSinceSend = sentAttachments.filter((attached) =>
          reclaimingFiles.current.has(attached.file_id),
        );
        if (reclaimedSinceSend.length > 0) {
          sentAttachments = sentAttachments.filter(
            (attached) => !reclaimingFiles.current.has(attached.file_id),
          );
          setMessages((prev) =>
            prev.map((message) =>
              message.id === userMessageId
                ? {
                    ...message,
                    attachedFiles:
                      sentAttachments.length > 0 ? sentAttachments : undefined,
                  }
                : message,
            ),
          );
          toast({
            title: "Attachment removed",
            description:
              "A file attached to this message was deleted, so it was not sent.",
          });
        }
        requestStarted = true;
        for (const attached of sentAttachments) {
          inFlightFileIds.current.add(attached.file_id);
        }
        const credentialRecoveryToken =
          ensureCredentialRecoveryToken(workflowPermanentId);
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
            attached_file_ids: sentAttachments.map(
              (attached) => attached.file_id,
            ),
            workflow_yaml: workflowYaml,
            mode: "build",
            cancel_token: cancelToken,
            idempotency_key: options.idempotencyKey ?? null,
            target_block_label: targetBlockLabel,
            product_action: productAction?.action ?? null,
            recording_evidence:
              productAction?.action === "refine_recording"
                ? useRecordingRefinementEvidenceStore
                    .getState()
                    .peek(productAction.nonce)
                : null,
            ...(options.recording ?? snapshotRecording()),
            selected_block_label: readSelectedBlockLabel(),
            keep_pending_proposal: Boolean(pendingProposalTurnId),
            supports_credential_pause: true,
            supports_credential_pause_recovery: Boolean(
              credentialRecoveryToken,
            ),
            credential_recovery_token: credentialRecoveryToken ?? undefined,
            supports_question_tool: true,
          } as WorkflowCopilotChatRequest,
          (payload) => {
            if (
              !isCopilotTurnCurrent(reservation) ||
              abortController.signal.aborted
            )
              return true;
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
                if (submittedSnapshot) submittedSnapshot.titlePersisted = true;
                onWorkflowPersisted?.(workflowPermanentId);
                // Backend already persisted it; reload reads canonical. This only
                // moves the live title bar, and never over a user-chosen name.
                withCopilotAcceptance(reservation, () =>
                  useWorkflowTitleStore
                    .getState()
                    .setTitleFromCopilotIfDefault(payload.title),
                );
                return false;
              case "credential_required":
                sawCredentialPause = true;
                setLivePauseFrame(payload);
                return false;
              case "credential_pause_resolved": {
                const pause = parseCredentialPause({
                  outcome: payload.outcome,
                  credentialId: payload.credential_id,
                });
                if (!pause || pause.outcome === "declined") return false;
                const resolution: CredentialResolution = {
                  outcome: pause.outcome,
                  credentialId: pause.credentialId ?? undefined,
                  name: payload.name ?? undefined,
                };
                setPauseCardResolutions((prev) =>
                  withCappedResolution(prev, payload.resume_token, resolution),
                );
                return false;
              }
              case "turn_start": {
                if (productAction?.action === "refine_recording") {
                  useRecordingRefinementEvidenceStore
                    .getState()
                    .take(productAction.nonce);
                  onInitialMessageConsumedRef.current?.();
                  setMessages((prev) =>
                    prev.map((message) =>
                      message.id === userMessageId &&
                      message.recordingRefinement
                        ? {
                            ...message,
                            recordingRefinement: {
                              ...message.recordingRefinement,
                              turnId: payload.turn_id,
                            },
                          }
                        : message,
                    ),
                  );
                }
                // A new turn can't carry the prior turn's dead resume_token.
                setLivePauseFrame(null);
                // Move the pre-submit canvas snapshot into the per-turn
                // map keyed by the BE-assigned turn_id; cap the map so a
                // long-running chat does not retain every turn's snapshot.
                const map = turnSnapshots.current;
                if (submittedSnapshot)
                  map.set(payload.turn_id, submittedSnapshot);
                pendingSubmitSnapshot.current = null;
                pendingSubmitSettings.current = undefined;
                while (map.size > MAX_TURN_SNAPSHOTS) {
                  const oldest = map.keys().next().value;
                  if (oldest === undefined) break;
                  map.delete(oldest);
                }
                latestTurnId.current = payload.turn_id;
                streamTurnId = payload.turn_id;
                // The server has saved these files on the turn. A same-tick duplicate queued behind it
                // shares their ids, so editing that duplicate must not let a removal delete them.
                for (const attached of sentAttachments) {
                  postedFileIds.current.add(attached.file_id);
                }
                // The chat this turn belongs to, read while the server is
                // announcing it. Reading the ref at arming time instead would
                // bind the poll to whatever chat the user switched to since.
                streamChatId = workflowCopilotChatIdRef.current;
                applyStoredNarrativeEvent(payload, EMPTY_NARRATIVE);
                return false;
              }
              case "design_start":
              case "design_end":
              case "codegen_progress":
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
                  const applied = applyWorkflowUpdate(
                    payload.workflow,
                    {
                      midTurnDraft: true,
                    },
                    reservation,
                    submittedSettings,
                  );
                  if (applied) {
                    if (submittedSnapshot)
                      submittedSnapshot.hadStagedDraft = true;
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
                // The stream ends normally on an error frame, so the catch-path restore never runs.
                if (streamTurnId === null) {
                  returnFilesToTray(sentAttachments);
                }
                handleError(payload, frozenNarrative);
                return true;
              }
              default:
                return false;
            }
          },
          { signal: abortController.signal },
        );
        // The streaming client resolves rather than throws on abort, so New chat, a chat switch, or
        // Stop before turn_start ends here, not in the catch.
        if (abortController.signal.aborted) {
          if (!requestStarted || sawTerminalFrame)
            finishRecordingRefinement("cancelled");
          returnPossiblySavedFiles();
        }
      } catch (error) {
        if (!isCopilotTurnCurrent(reservation)) return false;
        // A stream severed before turn_start may still have been saved by the server. An error frame
        // before turn_start is definitive and leaves the files deletable.
        if (abortController.signal.aborted) {
          returnPossiblySavedFiles();
          if (!requestStarted || sawTerminalFrame)
            finishRecordingRefinement("cancelled");
          return false;
        }
        definitiveRejection =
          streamTurnId === null &&
          error instanceof Error &&
          "status" in error &&
          typeof error.status === "number" &&
          [400, 401, 403, 404, 405, 413, 415, 422, 429].includes(error.status);
        returnPossiblySavedFiles();
        console.error("Failed to send message:", error);
        retainUnconfirmedOutcome();
        if (
          !definitiveRejection &&
          options.idempotencyKey !== undefined &&
          workflowCopilotChatId
        ) {
          toast({
            title: "Checking account selection",
            description: RECOVERY_IN_PROGRESS_MESSAGE,
            variant: "destructive",
          });
          await loadChatInPlace(workflowCopilotChatId);
        } else {
          const recovering =
            shouldArmRecovery() ||
            Boolean(
              recordingRefinement && requestStarted && !definitiveRejection,
            );
          if (!recovering) {
            finishRecordingRefinement("failed");
          }
          const errorMessage: ChatMessage = {
            id: Date.now().toString(),
            sender: "ai",
            content: recovering
              ? RECOVERY_IN_PROGRESS_MESSAGE
              : definitiveRejection && error instanceof Error
                ? error.message
                : SEND_FAILED_MESSAGE,
            recoveryTurnId: recovering
              ? (streamTurnId ?? cancelToken)
              : undefined,
          };
          recoveryNoticeId = errorMessage.id;
          setMessages((prev) => [...prev, errorMessage]);
          if (recovering && streamTurnId === null && recordingRefinement) {
            startPersistedRecordingTurnLookup();
          }
        }
        // A thrown stream never emits a terminal narrative event, so clear the
        // bubble or its Working/elapsed indicator would tick forever.
        setNarrative(EMPTY_NARRATIVE);
        setLivePauseFrame(null);
        return false;
      } finally {
        retainUnconfirmedOutcome();
        await terminalRecovery;
        if (
          captureLiveTurnRecovery.current === captureRecovery &&
          (!requestStarted ||
            sawTerminalFrame ||
            pendingCanonicalRecovery.current)
        )
          captureLiveTurnRecovery.current = null;
        sendFinished = true;
        if (
          recordingRefinement &&
          requestStarted &&
          streamTurnId === null &&
          shouldArmRecovery() &&
          !recoveryNoticeId
        ) {
          startPersistedRecordingTurnLookup();
        }
        abortController.signal.removeEventListener(
          "abort",
          releaseUnsentReservation,
        );
        if (requestStarted) {
          for (const attached of sentAttachments) {
            inFlightFileIds.current.delete(attached.file_id);
          }
        }
        unpostedSendsRef.current.delete(registeredAttachments);
        // A turn can change schedules through Copilot's tools and still end in an error or abort.
        void queryClient.invalidateQueries({
          queryKey: ["workflowSchedules", workflowPermanentId],
        });
        const current = isCopilotOwnerCurrent(reservation);
        const armRecovery = shouldArmRecovery();
        const retainReservation =
          armRecovery && pendingCanonicalRecovery.current !== null;
        if (!retainReservation) {
          finishCopilotAcceptance(reservation);
          if (copilotReservation.current === reservation)
            copilotReservation.current = null;
        }
        if (current) {
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
          buildFollowEngaged.current = false;
          if (armRecovery) {
            adoptRecoveryTurn = startRecoveryPoll(
              streamChatId ?? chatIdForRequest,
              streamTurnId,
              cancelToken,
              retainReservation ? reservation : undefined,
              sawCredentialPause,
              recordingRefinement ? userMessageId : undefined,
              sendPresentationGeneration,
            );
          }
        }
      }
      return true;
    },
    [
      acceptUnresolved,
      applyStoredNarrativeEvent,
      createCanonicalRecovery,
      onWorkflowPersisted,
      reserveCopilotTurn,
      isCopilotTurnCurrent,
      isCopilotOwnerCurrent,
      applyWorkflowUpdate,
      reconcileCanonicalWorkflow,
      armStop,
      authoringInProgress,
      backfillProposalRunFacts,
      credentialGetter,
      fetchRecordedActions,
      finalizeRecordedActionsPoll,
      attachments,
      recordingFocusOpen,
      pendingAttachments,
      focusTurnRun,
      followBuildLabel,
      inputValue,
      questionInteractions,
      handleQuestionAnswer,
      isSpeechListening,
      isLiveBrowserReady,
      liveBrowserSessionId,
      loadChatInPlace,
      pendingProposalTurnId,
      rememberTurnOwnedRun,
      startRecordedActionsPoll,
      stopAllRecordedActionsPolls,
      requiresLiveBrowser,
      resyncProposalFromChatRow,
      returnFilesToTray,
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
      // `handleSend` drops the send when the fence is up, and it does so AFTER this latch would
      // be taken - which greys the picker out for the turn without sending anything.
      if (
        hasPendingQuestion ||
        acceptUnresolved ||
        connectedAccountChoiceLatch.current !== null
      ) {
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
    [handleSend, hasPendingQuestion, acceptUnresolved],
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
    setBlockBuildArmNonce((nonce) => nonce + 1);
    clearPendingBlockBuild();
  }, [pendingBlockBuild, clearPendingBlockBuild]);

  useEffect(() => {
    if (blockBuildArmNonce === 0 || blockBuildMessageRef.current === null) {
      return;
    }
    if (acceptUnresolved) {
      return;
    }
    const message = blockBuildMessageRef.current;
    blockBuildMessageRef.current = null;
    // A prompt is already queued, so this send no-ops; a recording still owns
    // the canvas, so block rebuilds are refused. Disarm the block target
    // (else the queued drain inherits it) and clear the stuck generating state.
    if (queuedPromptRef.current || useRecordingStore.getState().isRecording) {
      blockBuildTargetLabelRef.current = null;
      finishBlockGenerating();
      return;
    }
    void handleSend(message, { deferReservation: true }).then((result) => {
      if (result !== false) return;
      blockBuildTargetLabelRef.current = null;
      finishBlockGenerating();
    });
  }, [blockBuildArmNonce, acceptUnresolved, handleSend, finishBlockGenerating]);

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
    if (
      !queuedPrompt ||
      // The drained send's store writes force a sync render before this drain's own
      // clear commits, so state can still hold a prompt the ref already released.
      queuedPromptRef.current !== queuedPrompt ||
      hasPendingQuestion ||
      queueDrainLocked ||
      acceptUnresolved ||
      authoringInProgress
    ) {
      return;
    }
    // isLoading (reactive state) is the in-flight signal here so the effect
    // re-runs when a turn ends; handleSend uses the synchronous ref instead.
    const lastTurn = lastTurnRef.current;
    const drainAction = resolveDrainAction({
      queuedReason: queuedPrompt.reason,
      inFlight: isLoading || recoveredTurnOwnerRef.current !== null,
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
        (queuedPrompt.audioBlob ?? null) === null &&
        sameFileIds(
          lastTurn?.attachmentIds,
          (queuedPrompt.attachments ?? []).map((attached) => attached.file_id),
        ) &&
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
      deferReservation: true,
      queuedMessageId: promptToSend.id,
      skipQueue: drainAction === "drain_skip_queue",
      audioBlob: promptToSend.audioBlob,
      idempotencyKey: promptToSend.idempotencyKey,
      selectedConnectedAccountId: promptToSend.selectedConnectedAccountId,
      attachments: promptToSend.attachments,
      recording: promptToSend.recording,
    }).catch((error) => {
      console.error("Queued send failed:", error);
    });
  }, [
    acceptUnresolved,
    authoringInProgress,
    handleSend,
    hasPendingQuestion,
    isLoading,
    liveBrowserSessionId,
    queueDrainLocked,
    queuedPrompt,
    updateQueuedPrompt,
    workflowPermanentId,
  ]);

  useEffect(() => {
    if (!autoSendMessage || hasAutoSentRef.current) {
      return;
    }
    if (
      isLoadingHistory ||
      isLoading ||
      !workflowPermanentId ||
      queuedPrompt ||
      workflowMutationLocked ||
      authoringInProgress ||
      acceptUnresolved
    ) {
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
    if (initialAction?.kind !== "refine_recording") {
      onInitialMessageConsumedRef.current?.();
    }
    if (initialAction) {
      productActionRef.current =
        initialAction.kind === "diagnose_run"
          ? {
              action: "diagnose_run",
              workflowRunId: initialAction.workflowRunId,
            }
          : { action: "refine_recording", nonce: initialAction.nonce };
    }
    handleSend(autoSendMessage, {
      deferReservation: true,
      ...(!initialAction
        ? {
            optimisticMessageId: initialHandoffMessageId,
            attachments: initialAttachments,
          }
        : initialAttachments && initialAttachments.length > 0
          ? { attachments: initialAttachments }
          : {}),
    }).catch((error) => {
      console.error("Auto-send failed:", error);
    });
  }, [
    handleSend,
    autoSendMessage,
    authoringInProgress,
    initialAttachments,
    initialAction,
    initialHandoffMessageId,
    acceptUnresolved,
    isLoading,
    isLoadingHistory,
    queuedPrompt,
    getSaveData,
    workflowPermanentId,
    workflowMutationLocked,
  ]);

  useEffect(() => {
    if (!autoSendMessage || initialAction || hasAutoSentRef.current) {
      return;
    }
    if (
      isLoadingHistory ||
      isLoading ||
      acceptUnresolved ||
      isWaitingForLiveBrowser ||
      workflowMutationLocked ||
      authoringInProgress ||
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
    authoringInProgress,
    initialAction,
    acceptUnresolved,
    isLoadingHistory,
    isLoading,
    isWaitingForLiveBrowser,
    queuedPrompt,
    getSaveData,
    workflowMutationLocked,
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

  // Thumbs stay visible only on the newest assistant turn; older turns, rated or not,
  // reveal them on hover so a long chat does not fill with controls.
  const latestRatableIndex = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const candidate = messages[i];
      if (candidate?.sender === "ai" && !candidate.kind) {
        return i;
      }
    }
    return -1;
  }, [messages]);

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
      ((canonicalRecoveryInFlight.current &&
        streamingAbortController.current !== null) ||
        (stopArmed &&
          narrative.terminal === null &&
          pendingCancelToken.current !== null)));
  // A render-phase write would let a discarded pass latch, and a passive effect
  // would leave Stop painted armed while cancelSend still reads false.
  useLayoutEffect(() => {
    turnObservablyRunningRef.current = turnObservablyRunning;
  }, [turnObservablyRunning]);

  const recoveryBanner = recoveryControls ? (
    <div
      role={outstandingAccept ? "status" : "alert"}
      className="space-y-2 rounded-md border p-3 text-sm"
    >
      <p>
        {recoveryControls.conflict
          ? "The saved workflow changed while you had local edits. Choose which changes to keep."
          : recoveryControls.cancelling
            ? "Cancelling the Copilot turn. Checking for saved changes before restoring your draft."
            : recoveryControls.reload
              ? "Could not confirm whether Copilot saved changes. Save is blocked. Retry or reload the saved workflow to check again."
              : "Copilot is checking for saved changes. Your draft is retained. Reject requests cancellation; changes already saved will be kept."}
      </p>
      <div className="flex gap-2">
        {recoveryControls.conflict ? (
          <>
            <Button
              variant="outline"
              size="sm"
              onClick={recoveryControls.conflict.keep}
            >
              Keep my edits
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={recoveryControls.conflict.discard}
            >
              Apply and discard my edits
            </Button>
          </>
        ) : (
          <>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                if (startupFailed) setStartupRetry((value) => value + 1);
                recoveryControls.retry();
              }}
            >
              Retry
            </Button>
            {recoveryControls.reload ? (
              <Button
                variant="outline"
                size="sm"
                onClick={recoveryControls.reload}
              >
                Reload
              </Button>
            ) : null}
            {!outstandingAccept ? (
              <Button
                variant="outline"
                size="sm"
                onClick={recoveryControls.reject}
              >
                Reject
              </Button>
            ) : null}
          </>
        )}
      </div>
    </div>
  ) : null;
  // The studio keeps closed panes mounted and CSS-hidden. Keep this controller
  // mounted with them until recording finalization completes; otherwise a pane
  // close would cancel the finalize timer and a later reopen could start a
  // second process_recording mutation from fresh component state.
  if ((!isOpen && !recordingAuthoringActive) || (docked && !portalTarget)) {
    return recoveryBanner
      ? createPortal(
          <div className="fixed bottom-4 right-4 z-[100] max-w-md rounded-lg border border-border bg-background shadow-lg">
            {recoveryBanner}
          </div>,
          document.body,
        )
      : null;
  }

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
  const gateOwnerTurnId =
    gateFailure?.kind === "saved"
      ? gateFailure.ownerTurnId
      : pendingProposalTurnId;
  const gateOwnerIndex = gateOwnerTurnId
    ? findLastIndexOfTurn(messages, gateOwnerTurnId)
    : -1;
  const gateIndex = gateOwnerIndex >= 0 ? gateOwnerIndex : lastTurnIndex;
  const gateOwnerMessage = messages[gateIndex];
  const gateOwnerNarrative = gateOwnerMessage?.narrative;
  const gateOwnerRendersInline = Boolean(
    gateOwnerMessage &&
    gateOwnerMessage.sender === "ai" &&
    gateOwnerMessage.kind !== "run_lifecycle" &&
    gateOwnerMessage.kind !== "status_notice" &&
    !(
      gateOwnerNarrative &&
      !(
        shouldShowDiffCard(gateOwnerNarrative) ||
        (gateOwnerNarrative.turnId !== null &&
          gateOwnerNarrative.turnId === pendingProposalTurnId)
      )
    ),
  );
  // Mid-turn Accept would be clobbered by the in-flight turn's terminal
  // restore, so gate actions wait for idle.
  // Is there anything for the gate to show? A staged proposal, or one of the three gates that
  // outlive one - `saved`, `recover`, `changed` - keyed on the gate rather than on how the subject
  // became null; actionability does not follow, since `ReviewGateCard` locks its action row
  // whenever there is no proposal to act on.
  const gateHasSubject =
    Boolean(proposedWorkflow) ||
    (Boolean(outstandingAccept) && !unattributedClaim) ||
    gateFailure?.kind === "saved" ||
    gateFailure?.kind === "recover" ||
    gateFailure?.kind === "changed";
  // The action row stays locked for `saved` by its own fieldset, so this enables the exit only.
  const gateActionable = gateHasSubject && !isLoading && !isLoadingHistory;
  const turningOffThisChat =
    workflowCopilotChatId !== null &&
    (turningOffCounts.get(workflowCopilotChatId) ?? 0) > 0;
  // Only the two accepts wait for Turn off; Review and Reject write no auto_accept.
  const gateAcceptsEnabled = !turningOffThisChat;
  // A staged attachment counts as content: with only a file in the tray the button would
  // otherwise read as Stop during a turn, and clicking Send would cancel the turn instead.
  const hasComposerText =
    inputValue.trim().length > 0 ||
    attachments.length > 0 ||
    pendingAttachments.some((item) => item.status === "uploading");
  const recordingRefinementInFlight = messages.some((message) => {
    const refinement = message.recordingRefinement;
    if (refinement?.status !== "working") return false;
    return (
      message.id === pendingMessageId.current ||
      (recoveredTurnOwnerRef.current !== null &&
        refinement.turnId === recoveredTurnOwnerRef.current)
    );
  });
  // Recording refinement now settles into the same conversation rhythm as any
  // other Copilot turn: its receipt stays in the transcript while the existing
  // working row reports that the turn is still live.
  const showWorkingRow = isLoading;
  // A live_browser-reason queued prompt parks with no active turn to stop, so
  // an empty composer's morph button would render as a guaranteed no-op "Send".
  // With text typed it does act — it adds to the parked prompt.
  const waitingOnQueueOnly =
    queuedPrompt?.reason === "live_browser" && !hasComposerText;
  // A Home handoff already shows its queued prompt as a bubble with its own footer, so the strip
  // would repeat it.
  const showQueuedStrip = Boolean(
    queuedPrompt && !messages.some((message) => message.id === queuedPrompt.id),
  );
  const showsStopGlyph =
    isStopping || (turnObservablyRunning && !hasComposerText);
  const authoringBlocksComposerAction = authoringInProgress && !showsStopGlyph;
  // Sent, no frame yet: the control reports the wait rather than an action, and
  // cancelSend's own guard is what makes a press in this window issue no cancel.
  const turnPendingFirstFrame =
    isLoading && !turnObservablyRunning && narrative.terminal === null;
  const morphButtonPending = turnPendingFirstFrame && !hasComposerText;
  const morphButtonLabel = authoringBlocksComposerAction
    ? "Send disabled — finish the current authoring action"
    : isStopping
      ? "Stopping…"
      : waitingOnQueueOnly
        ? "Send disabled — waiting for live browser"
        : morphButtonPending
          ? "Starting…"
          : queuedPrompt && hasComposerText
            ? queuedPrompt.origin === "typed"
              ? "Add to the queued message"
              : "Replace queued message"
            : !turnObservablyRunning
              ? isLoading
                ? "Queue for next turn"
                : "Send"
              : hasComposerText
                ? "Queue for next turn"
                : "Stop";

  const renderQuestionCard = (interaction: QuestionInteraction) => (
    <QuestionPartsCard
      key={interaction.interaction_id}
      interaction={interaction}
      // The answer path is fenced in `handleQuestionAnswer` and returns false silently, so a card
      // that stays clickable under an unresolved Accept reports a submit that never happened -
      // the false receipt this slice exists to remove. Same gate and same reason as Cancel.
      disabled={isSubmittingQuestion || acceptUnresolved}
      lockReason={acceptUnresolved ? acceptHoldReason : null}
      onAnswer={(response) => void handleQuestionAnswer(interaction, response)}
    />
  );

  const uploadSOPDisabled = !onUploadSOP || !canUploadSOP || isUploadingSOP;
  const recordTaskDisabled = !onRecordTask || !canRecordTask || isUploadingSOP;
  const recordingChapterController = recordingAuthoringActive ? (
    <RecordingPanel
      key="live-recording-chapter"
      ref={recordingChapterRef}
      browserSessionId={liveBrowserSessionId ?? null}
      expanded={recordingFocusOpen}
      collapsed={recordingCollapsed}
      onCollapsedChange={setRecordingCollapsed}
      onExpandedChange={setRecordingFocusOpen}
      onBackToChat={() => setRecordingFocusOpen(false)}
      portalTarget={
        recordingFocusOpen
          ? recordingFocusPortalRef.current
          : recordingInlinePortalEl
      }
      suggestionPortalTarget={recordingSuggestionPortalEl}
    />
  ) : null;
  const recordingChapterHost = recordingAuthoringActive ? (
    <div key="live-recording-chapter-host" ref={setRecordingInlinePortalEl} />
  ) : null;
  const recordingSuggestionHost = recordingAuthoringActive ? (
    <div
      key="live-recording-suggestion-host"
      ref={setRecordingSuggestionPortalEl}
      data-testid="recording-suggestion-host"
    />
  ) : null;

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
              disabled={headerControlsDisabled}
              lockedReason={acceptHoldReason ?? undefined}
            />
            <button
              type="button"
              disabled={acceptHoldReason !== null}
              title={acceptHoldReason ?? undefined}
              onClick={handleNewChat}
              onMouseDown={(e) => e.stopPropagation()}
              className="rounded border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-accent-foreground disabled:opacity-50"
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
        {recordingChapterController}
        <div
          ref={recordingFocusPortalRef}
          className={cn(
            "absolute inset-0 z-30 bg-slate-elevation1",
            !recordingFocusOpen && "hidden",
          )}
        />
        <div
          ref={scrollRef}
          className={cn(
            "h-full overflow-y-auto p-4",
            recordingFocusOpen && "pointer-events-none invisible",
          )}
        >
          <div className="space-y-3">
            {!isLoadingHistory &&
            messages.length === 0 &&
            !isLoading &&
            !recordingAuthoringActive ? (
              <div className="flex flex-col gap-6 px-1 pt-2 text-sm text-muted-foreground">
                <div>
                  <p className="text-base font-semibold text-foreground">
                    What should this agent do?
                  </p>
                  <p className="mt-1.5 leading-relaxed">
                    Describe the goal and the site, and mention any login it
                    needs.
                  </p>
                  <p className="mt-2 text-xs text-muted-foreground/80">
                    Try “Find the top post on Hacker News today”
                  </p>
                </div>

                <div className="[container-name:copilot-actions] [container-type:inline-size]">
                  <p className="text-xs text-muted-foreground/80">
                    Or start from
                  </p>
                  <div className="mt-2 grid grid-cols-1 gap-2 [@container_copilot-actions_(min-width:440px)]:grid-cols-2">
                    <TooltipProvider>
                      <ControlTooltip
                        content={
                          uploadSOPDisabled
                            ? (authoringUnavailableReason ??
                              "SOP upload is not available right now")
                            : "Upload a procedure as a PDF"
                        }
                        blocked={uploadSOPDisabled}
                        side="top"
                        wrapperClassName="min-w-0 w-full"
                      >
                        <button
                          type="button"
                          aria-label="Upload an SOP"
                          className="flex w-full min-w-0 items-center gap-3 rounded-lg border border-border bg-slate-elevation3 p-3 text-left transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50"
                          disabled={uploadSOPDisabled}
                          onClick={() => {
                            if (refuseMutationDuringYamlCommit()) return;
                            sopFileInputRef.current?.click();
                          }}
                        >
                          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border bg-slate-elevation2">
                            <UploadIcon className="h-4 w-4" />
                          </span>
                          <span className="min-w-0">
                            <span className="block font-medium text-foreground">
                              {isUploadingSOP
                                ? "Uploading SOP…"
                                : "Upload an SOP"}
                            </span>
                            <span className="mt-0.5 block text-xs leading-snug text-muted-foreground">
                              A PDF of the procedure
                            </span>
                          </span>
                        </button>
                      </ControlTooltip>
                    </TooltipProvider>
                    <TooltipProvider>
                      <ControlTooltip
                        content={
                          recordTaskDisabled
                            ? (authoringUnavailableReason ??
                              "Record Task is available when the browser is ready")
                            : "Skyvern records your clicks, typing, and navigation, then turns them into workflow steps"
                        }
                        blocked={recordTaskDisabled}
                        side="top"
                        wrapperClassName="min-w-0 w-full"
                      >
                        <button
                          type="button"
                          aria-label="Record task"
                          className="flex w-full min-w-0 items-center gap-3 rounded-lg border border-border bg-slate-elevation3 p-3 text-left transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50"
                          disabled={recordTaskDisabled}
                          onClick={() => {
                            if (refuseMutationDuringYamlCommit()) return;
                            onRecordTask?.();
                          }}
                        >
                          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border bg-slate-elevation2">
                            <span className="h-2.5 w-2.5 rounded-full bg-red-500" />
                          </span>
                          <span className="min-w-0">
                            <span className="block font-medium text-foreground">
                              Record task
                            </span>
                            <span className="mt-0.5 block text-xs leading-snug text-muted-foreground">
                              Do it once in the browser
                            </span>
                          </span>
                        </button>
                      </ControlTooltip>
                    </TooltipProvider>
                  </div>
                </div>
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
                  message.kind === "recording_refinement" &&
                  message.recordingRefinement
                ) {
                  return (
                    <RecordingRefinementProgressCard
                      key={message.id}
                      awaitingReview={Boolean(
                        proposedWorkflow &&
                        pendingProposalTurnId ===
                          message.recordingRefinement.turnId,
                      )}
                      {...message.recordingRefinement}
                    />
                  );
                }
                if (
                  message.kind === "run_lifecycle" ||
                  message.sender === "product"
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
                // Rated by turnId while live (the row id is unknown until a history load) and by
                // messageId after a reload. Both render sites below use this one control.
                const ratable = message.sender === "ai" && !message.kind;
                const feedbackTurnId = ratable
                  ? (message.narrative?.turnId ?? null)
                  : null;
                const feedbackControl =
                  ratable && (feedbackTurnId || message.messageId) ? (
                    <FeedbackThumbs
                      rating={message.feedback?.rating ?? null}
                      reason={message.feedback?.reason ?? null}
                      subtle={index !== latestRatableIndex}
                      prompt={
                        message.narrative?.turnFacts?.runId
                          ? "Did this do what you asked?"
                          : undefined
                      }
                      onRate={(rating, reason) =>
                        rateAssistantTurn(
                          message.id,
                          {
                            messageId: message.messageId,
                            turnId: feedbackTurnId,
                          },
                          rating,
                          reason,
                        )
                      }
                    />
                  ) : null;
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
                      className="group/turn flex flex-col gap-2"
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
                            acceptUnresolved ||
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
                              acceptUnresolved ||
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
                        <div className="space-y-2">
                          {index === gateIndex && pendingProposalRun ? (
                            <ProposalRunFactsLine facts={pendingProposalRun} />
                          ) : null}
                          <ReviewGateCard
                            turn={message.narrative}
                            pending={index === gateIndex && gateHasSubject}
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
                            hasProposal={Boolean(proposedWorkflow)}
                            acceptsEnabled={gateAcceptsEnabled}
                            onAccept={() => handleAcceptWorkflow()}
                            onAlwaysAccept={() => handleAcceptWorkflow(true)}
                            onReject={handleRejectWorkflow}
                            onReview={() =>
                              proposedWorkflow &&
                              handleReviewWorkflow(proposedWorkflow)
                            }
                            onTestEndToEnd={handleTestEndToEnd}
                            accepting={isAccepting}
                            failure={gateFailureKind}
                            onRetry={
                              gateFailureRetryable
                                ? retryGateFailure
                                : undefined
                            }
                            gateId={
                              turnId ? `copilot-gate-${turnId}` : undefined
                            }
                            flash={
                              turnId !== null && turnId === gateFlashTurnId
                            }
                          />
                        </div>
                      ) : null}
                      {!isLoadingHistory &&
                      isLastMessage &&
                      shouldShowConfirmCard(message.narrative) ? (
                        <ConfirmCard
                          disabled={acceptUnresolved}
                          lockReason={acceptHoldReason}
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
                        // The persisted pause verdict outranks the optimistic click, so a pick the server
                        // did not admit never reads as connected; the click's name survives via pauseCardResolutions.
                        const resolvedOutcome =
                          historicalCredentialOutcome(
                            message.narrative,
                            pauseCardResolutions,
                          ) ?? localResolution;
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
                      {message.narrative.terminal ? feedbackControl : null}
                    </div>
                  );
                }
                const isGateOwnerOrLast = index === gateIndex && gateHasSubject;
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
                      queuedPrompt?.id === message.id
                        ? {
                            text:
                              queuedPrompt.reason === "working"
                                ? "Queued — sends when this turn finishes."
                                : "Prompt queued. Waiting for live browser...",
                            onCancel: () => restoreQueuedPromptToComposer(),
                          }
                        : null
                    }
                    footer={
                      isGateOwnerOrLast || feedbackControl ? (
                        <div className="w-full space-y-2">
                          {isGateOwnerOrLast && pendingProposalRun ? (
                            <ProposalRunFactsLine facts={pendingProposalRun} />
                          ) : null}
                          {isGateOwnerOrLast ? (
                            <ReviewGateCard
                              pending
                              verdict={getReviewGateVerdict(
                                gateOwnerNarrative,
                                proposedWorkflow,
                              )}
                              settled={null}
                              actionsEnabled={gateActionable}
                              hasProposal={Boolean(proposedWorkflow)}
                              acceptsEnabled={gateAcceptsEnabled}
                              onAccept={() => handleAcceptWorkflow()}
                              onAlwaysAccept={() => handleAcceptWorkflow(true)}
                              onReject={handleRejectWorkflow}
                              onReview={() =>
                                proposedWorkflow &&
                                handleReviewWorkflow(proposedWorkflow)
                              }
                              onTestEndToEnd={handleTestEndToEnd}
                              accepting={isAccepting}
                              failure={gateFailureKind}
                              onRetry={
                                gateFailureRetryable
                                  ? retryGateFailure
                                  : undefined
                              }
                            />
                          ) : null}
                          {feedbackControl}
                        </div>
                      ) : null
                    }
                  />
                );
              })();
              const questions = questionInteractions
                .filter((item) => item.turn_id === message.narrative?.turnId)
                .map(renderQuestionCard);
              const rows = [...questions, rendered];
              return recordingChapterHost && recordingBoundary === index
                ? [recordingChapterHost, ...rows]
                : rows;
            })}
            {recordingChapterHost &&
            (recordingBoundary === null || recordingBoundary >= messages.length)
              ? recordingChapterHost
              : null}
            {startupPending && startupFailed && !recoveryControls ? (
              <div
                role="status"
                className="space-y-2 rounded-md border p-3 text-sm"
              >
                <p>
                  Could not check saved Copilot changes. Save and chat actions
                  are paused.
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setStartupRetry((value) => value + 1)}
                >
                  Retry
                </Button>
              </div>
            ) : null}
            {recoveryBanner}
            {gateHasSubject && !gateOwnerRendersInline ? (
              <div className="space-y-2">
                {pendingProposalRun ? (
                  <ProposalRunFactsLine facts={pendingProposalRun} />
                ) : null}
                <ReviewGateCard
                  pending
                  verdict={getReviewGateVerdict(undefined, proposedWorkflow)}
                  settled={null}
                  actionsEnabled={gateActionable}
                  hasProposal={Boolean(proposedWorkflow)}
                  acceptsEnabled={gateAcceptsEnabled}
                  onAccept={() => handleAcceptWorkflow()}
                  onAlwaysAccept={() => handleAcceptWorkflow(true)}
                  onReject={handleRejectWorkflow}
                  onReview={
                    proposedWorkflow
                      ? () => handleReviewWorkflow(proposedWorkflow)
                      : // Reached in the gates that outlive the proposal they were opened over.
                        // There is nothing staged to open, and `hasProposal` locks the fieldset
                        // Review sits in, so nothing in the UI calls this.
                        () => {}
                  }
                  onTestEndToEnd={handleTestEndToEnd}
                  accepting={isAccepting}
                  failure={gateFailureKind}
                  onRetry={gateFailureRetryable ? retryGateFailure : undefined}
                />
              </div>
            ) : null}
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
                // Cancelling reloads the turn, which can stage a newer proposal for a pending
                // Accept to overwrite. Same reason the answer path is fenced.
                disabled={isLoading || acceptUnresolved}
                title={acceptHoldReason ?? undefined}
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
              recordingRefinementInFlight ? null : (
                <InstantAckPlaceholder />
              )
            ) : null}
            {/*
            Bottom in-flight narrative bubble. Suppressed once the terminal
            RESPONSE has frozen the narrative into the latest AI message —
            otherwise the same turn would render twice.
          */}
            {!isLoadingHistory &&
              recoveredPauseFrames
                .filter(
                  (frame) =>
                    frame.workflow_copilot_chat_id === workflowCopilotChatId &&
                    frame.turn_id !== livePauseFrame?.turn_id,
                )
                .map((frame) => (
                  <CredentialCard
                    key={frame.resume_token}
                    frame={liveFrameToCardFrame(frame)}
                    mode="inline-pause"
                    reloadKey={credentialsReloadKey}
                    resolvedOutcome={pauseCardResolutions[frame.resume_token]}
                    onConnect={(credentialId, name) =>
                      credentialId
                        ? void respondToCredentialPause(
                            frame,
                            "connected",
                            credentialId,
                            name,
                          )
                        : openCredentialModal(frame, frame.turn_id)
                    }
                    onUpdateCredential={(credential) =>
                      openCredentialModal(
                        frame,
                        frame.turn_id,
                        false,
                        credential,
                      )
                    }
                    onSkip={() => void respondToCredentialPause(frame, "skip")}
                  />
                ))}
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
                    key={livePauseFrame.resume_token}
                    frame={liveFrameToCardFrame(livePauseFrame)}
                    mode="inline-pause"
                    reloadKey={credentialsReloadKey}
                    resolvedOutcome={
                      pauseCardResolutions[livePauseFrame.resume_token]
                    }
                    onUpdateCredential={(credential) =>
                      openCredentialModal(
                        livePauseFrame,
                        livePauseFrame.turn_id,
                        false,
                        credential,
                      )
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
            <WorkPlanCard items={workPlan} />
            {recordingSuggestionHost}
          </div>
        </div>
        {recordingChapterAboveViewport ? (
          <RecordingChapterProxy
            actionCount={recordingActionCount}
            newActionCount={Math.max(
              0,
              recordingActionCount - recordingProxyBaseline,
            )}
            lastActionTitle={lastRecordingActionTitle}
            isFinishing={recordingIsFinishing}
            previewOpen={recordingProxyPreviewOpen}
            onPreviewOpenChange={setRecordingProxyPreviewOpen}
            onJumpToChapter={jumpToRecordingChapter}
            onExpand={() => {
              setRecordingProxyBaseline(recordingActionCount);
              setRecordingFocusOpen(true);
            }}
          />
        ) : null}
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
        {(proposedWorkflow &&
          pendingProposalTurnId &&
          (gateOwnerIndex !== lastTurnIndex || isLoading)) ||
        (autoAccept && workflowCopilotChatId) ? (
          // One status strip: what Copilot is doing, separate from the mode control above it.
          <div className="mb-2 flex items-center gap-2 border-t border-border/60 pt-1.5 text-[10.5px] text-muted-foreground">
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
                className="flex min-w-0 items-center gap-1.5 rounded-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <span
                  className="h-1.5 w-1.5 shrink-0 rounded-full bg-sky-400"
                  aria-hidden="true"
                />
                <span className="truncate">1 proposal pending</span>
                <span className="shrink-0 text-foreground underline underline-offset-2">
                  Review
                </span>
              </button>
            ) : null}
            {autoAccept && workflowCopilotChatId ? (
              <div className="ml-auto flex min-w-0 items-center">
                <AutoAcceptChip
                  key={workflowCopilotChatId}
                  chatId={workflowCopilotChatId}
                  pendingFromChat={turningOffThisChat}
                  waitForAccept={async (turnOffChatId) => {
                    if (
                      startupPending ||
                      (deferredStartupClaim.current !== null &&
                        deferredStartupClaim.current.workflowPermanentId ===
                          workflowPermanentId)
                    ) {
                      throw new Error("Wait for the Copilot change to finish");
                    }
                    // An Accept clicked while Turn off waits joins the chain, so wait until it stops growing.
                    const deadline = Date.now() + ACCEPT_SETTLE_CEILING_MS;
                    let settled: Promise<void> | undefined;
                    do {
                      settled = acceptsInFlight.current.get(turnOffChatId);
                      if (
                        settled &&
                        !(await settledWithin(settled, deadline - Date.now()))
                      ) {
                        throw new Error("Accept is still running");
                      }
                    } while (
                      settled !== acceptsInFlight.current.get(turnOffChatId)
                    );
                    const pending = workflowPermanentId
                      ? useWorkflowYamlEditorStore.getState().pendingAccepts[
                          workflowPermanentId
                        ]
                      : undefined;
                    if (pending?.snapshot.chatId === turnOffChatId) {
                      throw new Error("Wait for the Copilot change to finish");
                    }
                  }}
                  onPendingChange={(pendingChatId, pending) =>
                    setTurningOffCounts((current) => {
                      const next = new Map(current);
                      const count =
                        (next.get(pendingChatId) ?? 0) + (pending ? 1 : -1);
                      if (count > 0) {
                        next.set(pendingChatId, count);
                      } else {
                        next.delete(pendingChatId);
                      }
                      return next;
                    })
                  }
                  onTurnedOff={(turnedOffChatId) => {
                    // The request can land after a switch to a chat whose setting it did not change.
                    if (turnedOffChatId !== workflowCopilotChatIdRef.current) {
                      return;
                    }
                    setAutoAcceptFromWrite(false);
                    textareaRef.current?.focus();
                  }}
                />
              </div>
            ) : null}
          </div>
        ) : null}
        {showWorkingRow ? (
          <CopilotWorkingStatus queued={Boolean(queuedPrompt)} />
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
        {hasPendingQuestion &&
        (attachments.length > 0 ||
          pendingAttachments.some((item) => item.status === "uploading")) ? (
          // An answer goes through the question flow, which carries no files, so a file staged
          // before the question arrived would otherwise look like part of the answer.
          <p className="mb-1 text-xs text-muted-foreground">
            Attached files will be sent with your next message, not with your
            answer.
          </p>
        ) : null}
        {attachments.length > 0 || pendingAttachments.length > 0 ? (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {attachments.map((attached) => (
              <AttachmentChip
                key={attached.file_id}
                filename={attached.filename}
                available={attached.available}
                onRemove={() => {
                  setAttachments((prev) =>
                    prev.filter((item) => item.file_id !== attached.file_id),
                  );
                  if (
                    !postedFileIds.current.has(attached.file_id) &&
                    !inFlightFileIds.current.has(attached.file_id) &&
                    // A page-exit delete is already on the wire for this one; a second request would
                    // race it, and the 404 loser would put the chip back for a file that is gone.
                    !reclaimingFiles.current.has(attached.file_id)
                  ) {
                    void removeUnsentUpload(attached.file_id).then(
                      (deleted) => {
                        if (deleted) {
                          return;
                        }
                        // The upload is still stored, so the chip comes back rather than leaving a
                        // file nothing can reach.
                        returnFilesToTray([attached]);
                        toast({
                          title: "Could not remove file",
                          description: `${attached.filename} is still attached. Try removing it again.`,
                        });
                      },
                    );
                  }
                }}
              />
            ))}
            {pendingAttachments.map((pending) => (
              <AttachmentChip
                key={pending.localId}
                filename={pending.filename}
                status={pending.status}
                error={pending.error}
                onRemove={
                  pending.status === "error"
                    ? () =>
                        setPendingAttachments((prev) =>
                          prev.filter(
                            (item) => item.localId !== pending.localId,
                          ),
                        )
                    : undefined
                }
              />
            ))}
          </div>
        ) : null}
        <span className="sr-only" aria-live="polite">
          {queuedPrompt && !showWorkingRow ? "Message queued" : ""}
        </span>
        {showQueuedStrip && queuedPrompt ? (
          <QueuedMessageStrip
            text={queuedPrompt.content}
            attachments={queuedPrompt.attachments ?? []}
            onEdit={
              queuedPrompt.origin === "product"
                ? undefined
                : () => restoreQueuedPromptToComposer()
            }
            onRemove={() => restoreQueuedPromptToComposer({ keepText: false })}
          />
        ) : null}
        <div
          role="group"
          aria-label="Copilot message composer"
          onDragEnter={handleComposerDragEnter}
          onDragOver={handleComposerDragOver}
          onDragLeave={handleComposerDragLeave}
          onDrop={handleComposerDrop}
          className={cn(
            "relative flex items-end gap-1.5 rounded-lg border border-input bg-slate-elevation2 py-1.5 pl-3 pr-2.5 transition-colors focus-within:border-ring",
            showQueuedStrip && "rounded-t-none",
          )}
        >
          {isFileDragging ? (
            <div
              role="status"
              aria-live="polite"
              className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center gap-2 rounded-lg border-2 border-dashed border-ring bg-slate-elevation2/95 text-sm font-medium text-foreground"
            >
              <FileIcon className="h-4 w-4" />
              Drop files to attach
            </div>
          ) : null}
          <textarea
            ref={setTextareaRef}
            placeholder={composerPlaceholder({
              queuedPrompt: queuedPrompt
                ? queuedPrompt.origin === "typed"
                  ? "add"
                  : "replace"
                : null,
              isLoading,
              isWaitingForLiveBrowser,
              latestTurnIsAsk,
            })}
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={handleKeyPress}
            disabled={authoringInProgress}
            rows={1}
            className="min-h-10 flex-1 resize-none border-0 bg-transparent py-2 text-sm leading-6 text-foreground placeholder:truncate placeholder:text-muted-foreground focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
            style={{
              minHeight: "40px",
              maxHeight: "150px",
              overflowY: "hidden",
            }}
          />
          <input
            ref={attachmentInputRef}
            type="file"
            accept={ATTACHMENT_ACCEPT}
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              // Reset first: picking the same file twice in a row fires no change event otherwise.
              event.target.value = "";
              if (file) void uploadAttachment(file);
            }}
          />
          <input
            ref={sopFileInputRef}
            type="file"
            accept=".pdf,application/pdf"
            aria-label="Choose an SOP PDF"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (!file) return;
              event.target.value = "";
              if (refuseMutationDuringYamlCommit() || uploadSOPDisabled) return;
              if (!file.name.toLowerCase().endsWith(".pdf")) {
                toast({
                  variant: "destructive",
                  title: "Invalid file type",
                  description: "Please select a PDF file",
                });
                return;
              }
              onUploadSOP?.(file);
            }}
          />
          <button
            type="button"
            onClick={() => attachmentInputRef.current?.click()}
            // An answer to a pending question is sent through the question flow, which carries
            // no files, so offering the control here would stage a file nothing can send.
            disabled={hasPendingQuestion || authoringInProgress}
            title={
              authoringInProgress
                ? "Finish the current authoring action before attaching a file"
                : hasPendingQuestion
                  ? "Answer the pending question before attaching a file"
                  : "Attach a file"
            }
            aria-label="Attach a file"
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-muted-foreground transition hover:bg-accent hover:text-accent-foreground"
          >
            <PlusIcon className="h-4 w-4" />
          </button>
          <SpeechInputButton
            isSupported={isSpeechSupported}
            isListening={isSpeechListening}
            isHearingSpeech={isSpeechHearing}
            disabled={authoringInProgress && !isSpeechListening}
            onToggle={toggleSpeech}
            className="h-8 w-8 rounded-full border-0 bg-transparent"
            iconClassName="h-3.5 w-3.5"
          />
          <TooltipProvider>
            <ControlTooltip
              content={
                acceptHoldReason ? (
                  <span className="block max-w-xs">{acceptHoldReason}</span>
                ) : (
                  morphButtonLabel
                )
              }
              blocked={
                waitingOnQueueOnly ||
                acceptUnresolved ||
                authoringBlocksComposerAction
              }
            >
              <button
                type="button"
                disabled={
                  waitingOnQueueOnly ||
                  isStopping ||
                  acceptUnresolved ||
                  authoringBlocksComposerAction
                }
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
          editingCredential={
            pendingCredentialConnect.current?.editingCredential
          }
          defaultTotpType={
            pendingCredentialConnect.current?.frame?.reason ===
            "credential_missing_totp"
              ? "authenticator"
              : undefined
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
