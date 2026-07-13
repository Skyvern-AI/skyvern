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
import { useParams } from "react-router-dom";
import {
  ReloadIcon,
  Cross2Icon,
  ChevronDownIcon,
  CheckIcon,
  ArrowUpIcon,
  StopIcon,
} from "@radix-ui/react-icons";
import { createPortal } from "react-dom";
import { stringify as convertToYAML } from "yaml";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import { useCopilotHeaderStore } from "@/store/useCopilotHeaderStore";
import { usePasteSkillHintStore } from "@/store/usePasteSkillHintStore";
import { WorkflowCreateYAMLRequest } from "@/routes/workflows/types/workflowYamlTypes";
import { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";
import {
  isBlockItem,
  WorkflowRunTimelineItem,
} from "@/routes/workflows/types/workflowRunTypes";
import { toast } from "@/components/ui/use-toast";
import { getSseClient } from "@/api/sse";
import {
  WorkflowCopilotCancelRequest,
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
  WorkflowCopilotRunOutcomeUpdate,
  WorkflowCopilotTurnStartUpdate,
  WorkflowCopilotWorkflowDraftUpdate,
  WorkflowCopilotChatSender,
  WorkflowCopilotChatRequest,
  WorkflowCopilotChatSummary,
  WorkflowCopilotClearProposedWorkflowRequest,
  WorkflowCopilotApplyProposedWorkflowRequest,
  WorkflowCopilotAudioUploadResponse,
} from "./workflowCopilotTypes";
import { WorkflowCopilotHistory } from "./WorkflowCopilotHistory";
import { shouldWaitForLiveBrowser } from "./browserReadiness";
import {
  QueuedPromptReason,
  resolveDrainAction,
  resolveSendAction,
} from "./sendQueue";
import { shouldAutoApplyWorkflowResponse } from "./proposalDisposition";
import { shouldArmDraftingGapTimer } from "./copilotPhases";
import { NarrativeView } from "./NarrativeView";
import { useRunLifecycleAnnouncements } from "./useRunLifecycleAnnouncements";
import { ConfirmCard, shouldShowConfirmCard } from "./cards/ConfirmCard";
import { DiffCard, shouldShowDiffCard } from "./cards/DiffCard";
import { FixCard, shouldShowFixCard } from "./cards/FixCard";
import { ReviewGateCard, getReviewGateVerdict } from "./cards/ReviewGateCard";
import {
  CopilotBlockActionsEvent,
  EMPTY_NARRATIVE,
  NarrativeEvent,
  RecordedActionSummary,
  TurnNarrativeState,
  applyNarrativeEvent,
  hydrateHistoryNarrative,
  parseUtcIsoMs,
} from "./narrativeState";
import { computeFollowSignature, useStickToBottom } from "./useStickToBottom";
import { useSpeechToTextField } from "@/hooks/useSpeechToTextField";
import { SpeechInputButton } from "@/components/SpeechInputButton";
import { useFeatureFlag, useFeatureFlagValue } from "@/hooks/useFeatureFlag";
import { useFeatureFlagEnabled } from "posthog-js/react";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from "@/components/ui/dropdown-menu";
import { cn, formatElapsedSeconds } from "@/util/utils";
import { COPILOT_UX_V1_FLAG } from "@/util/featureFlags";
import { ControlTooltip } from "@/routes/workflows/studio/ControlTooltip";
import { TooltipProvider } from "@/components/ui/tooltip";

// Cap on retained per-turn snap-back snapshots. A typical session has a
// handful of turns; this ceiling guards a runaway long-running chat.
const MAX_TURN_SNAPSHOTS = 20;

function normalizeInline(value: string | null | undefined): string | null {
  if (!value) return null;
  const trimmed = value.replace(/\s+/g, " ").trim();
  return trimmed.length > 0 ? trimmed : null;
}

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
    summary:
      normalizeInline(action.reasoning) ??
      normalizeInline(action.text) ??
      normalizeInline(action.response) ??
      normalizeInline(action.description),
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

type ComposerDefaultVariant =
  | "build"
  | "build_code"
  | "build_no_code"
  | "ask"
  | "ask_code";

function normalizeComposerDefaultVariant(
  variant: string | undefined,
): ComposerDefaultVariant {
  if (
    variant === "ask" ||
    variant === "ask_code" ||
    variant === "build_code" ||
    variant === "build_no_code"
  ) {
    return variant;
  }
  return "build";
}

function defaultVariantUsesCode(variant: ComposerDefaultVariant): boolean {
  return variant === "build_code" || variant === "ask_code";
}

function defaultCodeBlockRequestOverride(
  variant: string | undefined,
): boolean | null {
  if (variant === "build_code") {
    return true;
  }
  if (variant === "build" || variant === "build_no_code") {
    return false;
  }
  // Ask-only variants, including ask_code, do not send a build request override.
  return null;
}

// Ask's mark is a text dingbat; Build's is a color emoji that ModeGlyph flattens
// to a tone-adaptive monochrome silhouette so both read flat on the dark UI.
const ASK_GLYPH = "\u275D\uFE0E";
const BUILD_GLYPH = "\uD83D\uDC09";

function isPictographic(glyph: string): boolean {
  try {
    return /\p{Extended_Pictographic}/u.test(glyph);
  } catch {
    return false;
  }
}

function ModeGlyph({
  mode,
  tone = "light",
  glow = false,
}: {
  mode: "ask" | "build";
  tone?: "light" | "dark";
  glow?: boolean;
}) {
  const glyph = mode === "build" ? BUILD_GLYPH : ASK_GLYPH;
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
      <span
        className={cn(
          "relative",
          mode === "build" ? "text-[16px]" : "text-[15px]",
        )}
        style={{ lineHeight: 1, filter }}
      >
        {glyph}
      </span>
    </span>
  );
}

function ConvoAggregatePill({
  messages,
  isInFlight,
}: {
  messages: ChatMessage[];
  isInFlight: boolean;
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
  const anyError = turnsWithNarrative.some(
    (m) => m.narrative?.terminal === "error",
  );
  const status = isInFlight ? "In flight" : anyError ? "Halted" : "Done";
  const dotClass = isInFlight
    ? "bg-blue-400"
    : anyError
      ? "bg-rose-400"
      : "bg-emerald-400";
  return (
    <div className="flex justify-center pb-1">
      <span className="inline-flex items-center gap-2 rounded-full border border-slate-700 bg-slate-900/60 px-3 py-0.5 text-[11px] text-slate-300">
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
  // FE-synthetic run status line (never persisted, never sent to the LLM).
  kind?: "run_lifecycle";
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
// proposal actions / FixCard keep attaching to the last real turn.
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
  id: string;
  content: string;
  reason: QueuedPromptReason;
  audioBlob?: Blob | null;
  // The one-shot fix-origin signal travels with the prompt it was seeded for, so
  // discarding the queue (new chat, history load, agent switch) drops it too.
  fixOrigin?: boolean;
};

type SendOptions = {
  queuedMessageId?: string;
  skipQueue?: boolean;
  audioBlob?: Blob | null;
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
  | WorkflowCopilotRunOutcomeUpdate
  | WorkflowCopilotTurnStartUpdate
  | WorkflowCopilotDesignStartUpdate
  | WorkflowCopilotDesignEndUpdate
  | WorkflowCopilotWorkflowDraftUpdate;

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
    if (message.sender === "user") {
      return (
        <div className="flex justify-end">
          <div className="relative max-w-[85%] rounded-xl border border-white/5 bg-slate-elevation4 px-3.5 py-2.5 text-[13.5px] leading-[1.5] text-foreground">
            <p className="whitespace-pre-wrap pr-12">{message.content}</p>
            {queuedStatus ? (
              <div className="mt-2 flex items-center gap-1.5 border-t border-white/10 pt-2 text-[11.5px] text-slate-400">
                <ReloadIcon className="h-3 w-3 shrink-0 animate-spin" />
                <span className="min-w-0 flex-1 truncate">
                  {queuedStatus.text}
                </span>
                <button
                  type="button"
                  onClick={queuedStatus.onCancel}
                  title="Cancel queued message"
                  aria-label="Cancel queued message"
                  className="shrink-0 rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                >
                  <Cross2Icon className="h-3 w-3" />
                </button>
              </div>
            ) : message.timestamp ? (
              <span className="pointer-events-none absolute bottom-2 right-2 rounded bg-slate-elevation1/70 px-1.5 py-0.5 text-[10px] text-muted-foreground">
                {formatChatTimestamp(message.timestamp)}
              </span>
            ) : null}
          </div>
        </div>
      );
    }
    return (
      <div className="flex flex-col gap-2">
        <p className="whitespace-pre-wrap pl-1 text-[13px] leading-[1.55] text-slate-200">
          {message.content}
        </p>
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
  buttonRef?: React.RefObject<HTMLButtonElement>;
  liveBrowserSessionId?: string | null;
  workflowRunId?: string | null;
  requiresLiveBrowser?: boolean;
  isLiveBrowserReady?: boolean;
  initialMessage?: string;
  // Sent as fix_origin only on the initial turn; does not propagate to subsequent turns.
  initialMessageFixOrigin?: boolean;
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
  buttonRef,
  liveBrowserSessionId,
  workflowRunId: workflowRunIdProp,
  requiresLiveBrowser = false,
  isLiveBrowserReady = false,
  initialMessage,
  initialMessageFixOrigin,
  onInitialMessageConsumed,
  docked = false,
  chromeless = false,
  portalTarget,
}: WorkflowCopilotChatProps = {}) {
  const copilotV2Flag = useFeatureFlag("ENABLE_WORKFLOW_COPILOT_V2");
  const codeBlockModeFlag = useFeatureFlag("WORKFLOW_COPILOT_CODE_BLOCK_MODE");
  const codeBlockAccessFlag = useFeatureFlag("CODE_BLOCK_ACCESS");
  // Client-side PostHog eval (not the backend-served flags above).
  const copilotUxV1Enabled = useFeatureFlagEnabled(COPILOT_UX_V1_FLAG) ?? false;
  const copilotV2Enabled = copilotV2Flag === true;
  const codeBlockModeEnabled =
    codeBlockModeFlag === true && codeBlockAccessFlag === true;
  const defaultModeVariant = useFeatureFlagValue(
    "WORKFLOW_COPILOT_DEFAULT_MODE",
  );
  // S4: under the campaign flag, default straight to code-first (skipping the
  // WORKFLOW_COPILOT_DEFAULT_MODE A/B variant) whenever code-first is accessible.
  const codeFirstAccessible = copilotV2Enabled && codeBlockModeEnabled;
  const s4DefaultVariant: ComposerDefaultVariant = codeFirstAccessible
    ? "build_code"
    : "build";
  // The variant configures the initial default only when both gating flags are on.
  const effectiveDefaultVariant: ComposerDefaultVariant = copilotUxV1Enabled
    ? s4DefaultVariant
    : copilotV2Enabled && codeBlockModeEnabled
      ? normalizeComposerDefaultVariant(defaultModeVariant)
      : "build";
  // codeBlockRequestOverride keys off the same source effectiveDefaultVariant
  // used, not the raw flag value, so the S4 default stays internally consistent.
  const codeOverrideVariantSource = copilotUxV1Enabled
    ? s4DefaultVariant
    : defaultModeVariant;
  const [composerMode, setComposerMode] = useState<"ask" | "build">(() =>
    effectiveDefaultVariant === "ask" || effectiveDefaultVariant === "ask_code"
      ? "ask"
      : "build",
  );
  const [codeWorkflow, setCodeWorkflow] = useState(() =>
    defaultVariantUsesCode(effectiveDefaultVariant),
  );
  const [codeBlockRequestOverride, setCodeBlockRequestOverride] = useState<
    boolean | null
  >(() => defaultCodeBlockRequestOverride(codeOverrideVariantSource));
  // Flags arrive asynchronously from /customer; seed the default once they resolve, never again.
  const composerSeededRef = useRef(false);
  const flagsResolved =
    copilotV2Flag !== undefined &&
    codeBlockModeFlag !== undefined &&
    codeBlockAccessFlag !== undefined;
  useEffect(() => {
    if (composerSeededRef.current || !flagsResolved) {
      return;
    }
    composerSeededRef.current = true;
    setComposerMode(
      effectiveDefaultVariant === "ask" ||
        effectiveDefaultVariant === "ask_code"
        ? "ask"
        : "build",
    );
    setCodeWorkflow(defaultVariantUsesCode(effectiveDefaultVariant));
    setCodeBlockRequestOverride(
      defaultCodeBlockRequestOverride(codeOverrideVariantSource),
    );
  }, [
    flagsResolved,
    effectiveDefaultVariant,
    defaultModeVariant,
    copilotUxV1Enabled,
    codeOverrideVariantSource,
  ]);
  // Build can never be active unless the V2 flag is on.
  const isBuild = copilotV2Enabled && composerMode === "build";
  const codeToggleAllowed = effectiveDefaultVariant !== "build_no_code";
  // "Build with code" is offered as a third mode in the dropdown rather than a
  // separate toggle; the code state only renders on the button while in Build.
  const codeOptionAvailable =
    copilotV2Enabled && codeBlockModeEnabled && codeToggleAllowed;
  const codeStateActive = isBuild && codeWorkflow && codeOptionAvailable;
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
  const dismissPasteSkillHint = usePasteSkillHintStore((s) => s.dismiss);
  const [isLoading, setIsLoading] = useState(false);
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
  const streamingAbortController = useRef<AbortController | null>(null);
  // Synchronous in-flight gate. State (isLoading) lags a render behind, so a
  // rapid double-submit would run a stale closure and start a second stream;
  // this ref is set before the first await and read at the top of handleSend.
  const inFlightRef = useRef(false);
  // Synchronous mirror of queuedPrompt (like inFlightRef) so a same-tick double
  // submit can't queue twice and orphan the first message. Set via updateQueuedPrompt.
  const queuedPromptRef = useRef<QueuedPrompt | null>(null);
  const pendingMessageId = useRef<string | null>(null);
  const pendingCancelToken = useRef<string | null>(null);
  const cancelSafetyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const gateFlashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Backend cancel watcher polls Redis: a turn can complete normally between
  // the cancel POST and the watcher firing, so the frontend must remember it.
  const cancelInFlightController = useRef<AbortController | null>(null);
  const [workflowCopilotChatId, setWorkflowCopilotChatId] = useState<
    string | null
  >(null);
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
  // One-shot guard for the recorded-actions timeline fetch, keyed by
  // workflow_run_id — a run's evaluating and final verdict frames share an
  // id, so this stops the same run from being fetched twice.
  const fetchedActionRunIds = useRef<Set<string>>(new Set());
  // Run ids the copilot claimed via run_outcome — the turn narrates these
  // itself, so useRunLifecycleAnnouncements suppresses their lifecycle lines by
  // identity (an unrelated run seen in the same window must still be narrated).
  const turnOwnedRunIds = useRef<Set<string>>(new Set());
  useEffect(() => {
    workflowCopilotChatIdRef.current = workflowCopilotChatId;
  }, [workflowCopilotChatId]);
  useEffect(() => {
    return () => {
      streamingAbortController.current?.abort();
      if (cancelSafetyTimer.current !== null) {
        clearTimeout(cancelSafetyTimer.current);
        cancelSafetyTimer.current = null;
      }
      if (gateFlashTimer.current !== null) {
        clearTimeout(gateFlashTimer.current);
        gateFlashTimer.current = null;
      }
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
  const { workflowRunId: routeWorkflowRunId, workflowPermanentId } =
    useParams();
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
    workflowRunId: docked && copilotUxV1Enabled ? workflowRunId : undefined,
    // isLoading here, not inFlightRef: this hook needs a value React re-runs
    // its effect on when the turn ends, which a ref can't do. The one-render
    // lag that matters for the double-submit guard above doesn't matter here.
    turnInFlight: isLoading,
    turnOwnedRunIds,
    announce: announceRunLifecycle,
  });
  // Recorded actions arrive well after block_progress (they're persisted in
  // batch at block end), so fetch them once a run reaches adjudication
  // instead of waiting on the narrower block_progress/tool-call cadence.
  const maybeFetchRecordedActions = useCallback(
    async (payload: WorkflowCopilotRunOutcomeUpdate) => {
      if (!copilotUxV1Enabled) return;
      const runId = payload.workflow_run_id;
      if (
        !runId ||
        !workflowPermanentId ||
        fetchedActionRunIds.current.has(runId)
      ) {
        return;
      }
      const seen = fetchedActionRunIds.current;
      seen.add(runId);
      while (seen.size > MAX_TURN_SNAPSHOTS) {
        const oldest = seen.values().next().value;
        if (oldest === undefined) break;
        seen.delete(oldest);
      }
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
    [
      applyStoredNarrativeEvent,
      copilotUxV1Enabled,
      credentialGetter,
      workflowPermanentId,
    ],
  );
  // Explore/Draft boundary is unobservable (the LLM writes code with no
  // frames emitted); after DRAFTING_GAP_MS of silence with no pending block
  // run, assume Draft has started. Re-arms per narrative update; the reducer
  // guard makes a stale or double-fired timer a no-op.
  const DRAFTING_GAP_MS = 8000;
  useEffect(() => {
    if (!copilotUxV1Enabled) return;
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
  }, [narrative, applyStoredNarrativeEvent, copilotUxV1Enabled]);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const { getSaveData } = useWorkflowHasChangesStore();
  const hasInitializedPosition = useRef(false);
  const hasAutoSentRef = useRef(false);
  const isWaitingForLiveBrowser = shouldWaitForLiveBrowser({
    requiresLiveBrowser,
    isLiveBrowserReady,
  });
  // Reset on initialMessage change so a re-arrival of the prop (without a
  // remount) can fire auto-send again.
  useEffect(() => {
    hasAutoSentRef.current = false;
  }, [initialMessage]);
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
    enabled: isOpen && !queuedPrompt,
  });

  const updateQueuedPrompt = useCallback((next: QueuedPrompt | null) => {
    queuedPromptRef.current = next;
    setQueuedPrompt(next);
  }, []);

  const handleNewChat = () => {
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
    repin();
  };

  const applyHistoryResponse = useCallback(
    (
      data: WorkflowCopilotChatHistoryResponse,
      carryForwardLifecycle = true,
    ) => {
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
    // Only stable state setters are referenced, so the callback never needs to change.
    [],
  );

  const loadChatInPlace = useCallback(
    async (chatId: string) => {
      if (!workflowPermanentId) return;
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
  // proposal client-side (flag-on); re-fetch the chat row instead, since the
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
    updateQueuedPrompt,
    workflowPermanentId,
    applyHistoryResponse,
  ]);

  const cancelSend = useCallback(async () => {
    // Capture upfront so the 15s timer below can't latch onto a next turn's controller.
    const controllerAtCancel = streamingAbortController.current;
    if (!controllerAtCancel) return;

    const cancelToken = pendingCancelToken.current;
    pendingCancelToken.current = null;
    if (!cancelToken) return;

    cancelInFlightController.current = controllerAtCancel;

    const appendCancelledBubble = () => {
      setMessages((prev) => [
        ...prev,
        {
          id: `${Date.now()}-cancel`,
          sender: "ai",
          content: "Cancelled by user.",
          timestamp: new Date().toISOString(),
        },
      ]);
      // Otherwise the bubble freezes mid-state next to the Cancelled message.
      setNarrative(EMPTY_NARRATIVE);
    };

    try {
      const client = await getClient(credentialGetter, "sans-api-v1");
      await client.post<void>("/workflow/copilot/cancel", {
        cancel_token: cancelToken,
      } as WorkflowCopilotCancelRequest);
      // Safety net: if the SSE channel never resolves, surface a fallback
      // bubble and abort so handleSend's finally clears "Cancelling...".
      if (cancelSafetyTimer.current !== null) {
        clearTimeout(cancelSafetyTimer.current);
      }
      cancelSafetyTimer.current = setTimeout(() => {
        cancelSafetyTimer.current = null;
        if (streamingAbortController.current !== controllerAtCancel) return;
        appendCancelledBubble();
        controllerAtCancel.abort();
      }, 15_000);
    } catch (error) {
      // 503 (Redis disabled) or network failure: client-side abort still
      // gives the user immediate feedback; the backend will run to
      // completion in that environment. Log so we can spot it in dev.
      console.warn("Workflow copilot cancel POST failed", error);
      controllerAtCancel.abort();
      appendCancelledBubble();
    }
  }, [credentialGetter]);

  const cancelQueuedPrompt = useCallback(() => {
    if (!queuedPrompt) {
      return;
    }

    updateQueuedPrompt(null);
    // Drop the queued block-build target so it doesn't leak into the next message.
    // The fix-origin signal rode on the discarded prompt; clear the ref too in case
    // a future path set it without queuing.
    blockBuildTargetLabelRef.current = null;
    fixOriginPendingRef.current = false;
    setMessages((prev) =>
      prev.filter((message) => message.id !== queuedPrompt.id),
    );
    setInputValue(queuedPrompt.content);
    window.requestAnimationFrame(() => {
      textareaRef.current?.focus();
      adjustTextareaHeight();
    });
  }, [adjustTextareaHeight, queuedPrompt, updateQueuedPrompt]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || !isOpen) {
        return;
      }
      if (queuedPrompt) {
        cancelQueuedPrompt();
        return;
      }
      if (isLoading) {
        cancelSend();
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [cancelQueuedPrompt, cancelSend, isLoading, isOpen, queuedPrompt]);

  // Set by a block's "Generate" arm step so the next send scopes regeneration to that block.
  const blockBuildTargetLabelRef = useRef<string | null>(null);
  const fixOriginPendingRef = useRef(false);
  // True only while a block-build turn is actually in flight (not a turn it queued behind).
  const blockGenInFlightRef = useRef(false);

  const handleSend = useCallback(
    async (messageOverride?: string, options: SendOptions = {}) => {
      const candidate = messageOverride ?? inputValue;
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
        return;
      }
      if (!workflowPermanentId) {
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

      if (action === "queue_working" || action === "queue_live_browser") {
        const reason: QueuedPromptReason =
          action === "queue_working" ? "working" : "live_browser";
        const queuedId = options.queuedMessageId ?? crypto.randomUUID();
        // Move the pending fix-origin signal off the bare ref and onto the
        // queued prompt so a discard of the queue (new chat, history load)
        // can't leave it set to leak into the next, unrelated turn. The drain
        // restores it onto the ref before re-entering handleSend.
        const queuedFixOrigin = fixOriginPendingRef.current;
        fixOriginPendingRef.current = false;
        updateQueuedPrompt({
          id: queuedId,
          content: candidate,
          reason,
          audioBlob: messageAudioBlob,
          fixOrigin: queuedFixOrigin,
        });
        // First queue adds the user bubble; a re-queue (a working drain that
        // then had to wait for the browser) reuses the existing bubble.
        if (!options.queuedMessageId) {
          setMessages((prev) => [
            ...prev,
            { id: queuedId, sender: "user", content: candidate },
          ]);
        }
        if (!copilotUxV1Enabled) {
          setProposedWorkflow(null);
          setPendingProposalTurnId(null);
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
        sender: "user",
        content: candidate,
      };

      const cancelToken = crypto.randomUUID();
      pendingCancelToken.current = cancelToken;

      pendingMessageId.current = userMessageId;
      if (!options.queuedMessageId) {
        setMessages((prev) => [...prev, userMessage]);
      }
      if (!copilotUxV1Enabled) {
        setProposedWorkflow(null);
        setPendingProposalTurnId(null);
      }
      const messageContent = candidate;
      if (messageOverride === undefined && !options.queuedMessageId) {
        setInputValue("");
      }
      setIsLoading(true);
      inFlightRef.current = true;

      const abortController = new AbortController();
      streamingAbortController.current?.abort();
      streamingAbortController.current = abortController;

      try {
        const saveData = getSaveData();
        const workflowId = saveData?.workflow.workflow_id;
        let workflowYaml = "";
        let chatIdForRequest = workflowCopilotChatId;
        let audioArtifactId: string | null = null;

        if (!workflowId) {
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
            pin_saved_session_ip: saveData.settings.pinSavedSessionIp,
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
          setWorkflowCopilotChatId(response.workflow_copilot_chat_id);

          // freeze the current narrative state into the AI message
          // so per-block cards persist as the user scrolls past this turn.
          // Read via narrativeRef because this callback was closed over at
          // handleSend time (pre-turn_start), so the React state binding is
          // stale here.
          const liveNarrative = responseNarrative ?? narrativeRef.current;
          const hasNarrativePayload =
            response.narrative_payload !== null &&
            typeof response.narrative_payload === "object";
          const frozenNarrative: TurnNarrativeState | undefined =
            responseNarrative ??
            (liveNarrative.turnId !== null || hasNarrativePayload
              ? applyNarrativeEvent(
                  liveNarrative.turnId !== null
                    ? liveNarrative
                    : EMPTY_NARRATIVE,
                  response,
                )
              : undefined);

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
          } else if (copilotUxV1Enabled && pendingProposalTurnId) {
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
          const liveNarrative = errorNarrative ?? narrativeRef.current;
          const frozenNarrative: TurnNarrativeState | undefined =
            errorNarrative ??
            (liveNarrative.turnId !== null
              ? applyNarrativeEvent(liveNarrative, payload)
              : undefined);
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

        // Consume the one-shot fix-origin signal before any awaitable send step so a pre-stream
        // failure (e.g. getSseClient throwing) can't leave it set to leak into the next turn.
        const fixOrigin = fixOriginPendingRef.current;
        fixOriginPendingRef.current = false;
        const client = await getSseClient(credentialGetter);
        const targetBlockLabel = blockBuildTargetLabelRef.current;
        blockBuildTargetLabelRef.current = null;
        if (targetBlockLabel != null) {
          blockGenInFlightRef.current = true;
        }
        await client.postStreaming<WorkflowCopilotSsePayload>(
          "/workflow/copilot/chat-post",
          {
            workflow_id: workflowId,
            workflow_permanent_id: workflowPermanentId,
            workflow_copilot_chat_id: chatIdForRequest,
            workflow_run_id: workflowRunId,
            browser_session_id: liveBrowserSessionId ?? null,
            message: messageContent,
            audio_artifact_id: audioArtifactId,
            workflow_yaml: workflowYaml,
            mode: copilotV2Enabled ? composerMode : null,
            code_block:
              isBuild && codeBlockModeEnabled ? codeBlockRequestOverride : null,
            cancel_token: cancelToken,
            target_block_label: targetBlockLabel,
            fix_origin: fixOrigin,
            keep_pending_proposal:
              copilotUxV1Enabled && Boolean(pendingProposalTurnId),
          } as WorkflowCopilotChatRequest,
          (payload) => {
            switch (payload.type) {
              case "processing_update":
                handleProcessingUpdate(payload);
                return false;
              case "condensing":
                return false;
              case "tool_call":
              case "tool_result":
              case "narration":
              case "block_progress":
                applyStoredNarrativeEvent(payload);
                return false;
              case "run_outcome":
                applyStoredNarrativeEvent(payload);
                if (payload.workflow_run_id) {
                  const owned = turnOwnedRunIds.current;
                  owned.add(payload.workflow_run_id);
                  while (owned.size > MAX_TURN_SNAPSHOTS) {
                    const oldest = owned.values().next().value;
                    if (oldest === undefined) break;
                    owned.delete(oldest);
                  }
                }
                maybeFetchRecordedActions(payload);
                return false;
              case "turn_start": {
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
                applyStoredNarrativeEvent(payload, EMPTY_NARRATIVE);
                return false;
              }
              case "design_start":
              case "design_end":
                applyStoredNarrativeEvent(payload);
                return false;
              case "workflow_draft": {
                // Render the staged workflow on the canvas mid-turn. Only
                // mark the turn as having staged content if applyWorkflowUpdate
                // succeeds — a swallowed update would otherwise trigger a
                // spurious snap-back at terminal.
                if (payload.workflow) {
                  const applied = applyWorkflowUpdate(payload.workflow);
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
                handleResponse(payload, frozenNarrative);
                return true;
              }
              case "error": {
                const frozenNarrative = applyStoredNarrativeEvent(payload);
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
        const errorMessage: ChatMessage = {
          id: Date.now().toString(),
          sender: "ai",
          content: "Sorry, I encountered an error. Please try again.",
        };
        setMessages((prev) => [...prev, errorMessage]);
        // A thrown stream never emits a terminal narrative event, so clear the
        // bubble or its Working/elapsed indicator would tick forever.
        setNarrative(EMPTY_NARRATIVE);
      } finally {
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
        pendingMessageId.current = null;
        pendingCancelToken.current = null;
        setIsLoading(false);
      }
    },
    [
      applyStoredNarrativeEvent,
      applyWorkflowUpdate,
      autoAccept,
      codeBlockModeEnabled,
      codeBlockRequestOverride,
      composerMode,
      copilotUxV1Enabled,
      copilotV2Enabled,
      credentialGetter,
      getSaveData,
      inputValue,
      isSpeechListening,
      isBuild,
      isLiveBrowserReady,
      liveBrowserSessionId,
      maybeFetchRecordedActions,
      pendingProposalTurnId,
      requiresLiveBrowser,
      resyncProposalFromChatRow,
      stopSpeech,
      takeSpeechAudioBlob,
      updateQueuedPrompt,
      uploadDictationAudio,
      workflowCopilotChatId,
      workflowPermanentId,
      workflowRunId,
    ],
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
    setComposerMode("build");
    setCodeWorkflow(true);
    setCodeBlockRequestOverride(true);
    setBlockBuildArmNonce((nonce) => nonce + 1);
    clearPendingBlockBuild();
  }, [pendingBlockBuild, clearPendingBlockBuild]);

  useEffect(() => {
    if (blockBuildArmNonce === 0 || blockBuildMessageRef.current === null) {
      return;
    }
    if (composerMode !== "build" || !codeWorkflow) {
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
  }, [
    blockBuildArmNonce,
    composerMode,
    codeWorkflow,
    handleSend,
    finishBlockGenerating,
  ]);

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
      void cancelSend();
    }
  }, [blockCancelNonce, cancelSend, updateQueuedPrompt]);

  const handleKeyPress = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  useEffect(() => {
    // isLoading (reactive state) is the in-flight signal here so the effect
    // re-runs when a turn ends; handleSend uses the synchronous ref instead.
    const drainAction = resolveDrainAction({
      queuedReason: queuedPrompt?.reason ?? null,
      inFlight: isLoading,
      hasLiveBrowserSession: Boolean(liveBrowserSessionId),
      hasWorkflowPermanentId: Boolean(workflowPermanentId),
    });
    if (!queuedPrompt || drainAction === "wait") {
      return;
    }

    const promptToSend = queuedPrompt;
    // Clear before re-entering handleSend so a 'send' resolution leaves no
    // stale queued prompt for the effect to re-drain in a loop. A working
    // prompt that still needs the browser re-queues under the same id and
    // drains via the live_browser path once the session arrives.
    updateQueuedPrompt(null);
    // Restore the fix-origin signal the prompt carried so the drained turn
    // sends it; a re-queue (working → live_browser) re-captures it off the ref.
    fixOriginPendingRef.current = promptToSend.fixOrigin ?? false;
    handleSend(promptToSend.content, {
      queuedMessageId: promptToSend.id,
      skipQueue: drainAction === "drain_skip_queue",
      audioBlob: promptToSend.audioBlob,
    }).catch((error) => {
      console.error("Queued send failed:", error);
    });
  }, [
    handleSend,
    isLoading,
    liveBrowserSessionId,
    queuedPrompt,
    updateQueuedPrompt,
    workflowPermanentId,
  ]);

  useEffect(() => {
    if (!initialMessage || hasAutoSentRef.current) {
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
    if (initialMessageFixOrigin) {
      fixOriginPendingRef.current = true;
    }
    handleSend(initialMessage).catch((error) => {
      console.error("Auto-send failed:", error);
    });
  }, [
    handleSend,
    initialMessage,
    initialMessageFixOrigin,
    isLoading,
    isLoadingHistory,
    queuedPrompt,
    getSaveData,
    workflowPermanentId,
  ]);

  useEffect(() => {
    if (!initialMessage || hasAutoSentRef.current) {
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
        description:
          "The copilot was not ready in time — please retype your prompt.",
        variant: "destructive",
      });
    }, AUTO_SEND_TIMEOUT_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, [
    initialMessage,
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

  if (!isOpen) {
    return null;
  }

  // Input stays usable while Copilot works; only a parked queued prompt
  // disables it (the message is already captured).
  const inputDisabled = Boolean(queuedPrompt);
  const queuedPromptWaitingStatus =
    queuedPrompt?.reason === "working"
      ? "Queued — sends when this turn finishes."
      : "Prompt queued. Waiting for live browser...";
  // queuedPromptWaitingStatus already surfaces via the composer chip
  // (working reason) or the queued bubble's footer (live_browser reason);
  // don't duplicate it as a second status line above the composer.
  const browserStatusText = queuedPrompt
    ? copilotUxV1Enabled && copilotV2Enabled
      ? null
      : queuedPromptWaitingStatus
    : isLoading
      ? "Copilot is working. Your next send will wait for the next turn."
      : isWaitingForLiveBrowser
        ? "Live browser is starting. Your next send will wait until it connects."
        : null;
  const inputStatusText = isSpeechListening
    ? browserStatusText
      ? `Listening… · ${browserStatusText}`
      : "Listening…"
    : browserStatusText;
  const lastTurnIndex = findLastTurnIndex(messages);
  // A bypassed proposal's gate stays attached to its owning turn (not
  // necessarily the last message) so a chip can jump back to it.
  const gateOwnerIndex = pendingProposalTurnId
    ? findLastIndexOfTurn(messages, pendingProposalTurnId)
    : -1;
  const gateIndex = gateOwnerIndex >= 0 ? gateOwnerIndex : lastTurnIndex;
  // Mid-turn Accept would be clobbered by the in-flight turn's terminal
  // restore, so gate actions wait for idle.
  const gateActionable = Boolean(proposedWorkflow) && !isLoading;
  const hasComposerText = inputValue.trim().length > 0;
  // A live_browser-reason queued prompt parks with no active turn to stop and
  // a disabled, emptied textarea — the morph button would otherwise render as
  // a live "Send" that's a guaranteed no-op; only the Queued chip's ✕ acts.
  const waitingOnQueueOnly = queuedPrompt?.reason === "live_browser";
  const morphButtonLabel = waitingOnQueueOnly
    ? "Send disabled — waiting for live browser"
    : !isLoading
      ? "Send"
      : hasComposerText
        ? "Queue for next turn"
        : "Stop";
  // Shared between the legacy fused split-button and the S4 mode pill so the
  // three options never drift between the two composer treatments.
  const modeMenuItems = (
    <>
      <DropdownMenuItem
        aria-label="Ask"
        onSelect={() => {
          setComposerMode("ask");
          setCodeWorkflow(false);
          setCodeBlockRequestOverride(null);
        }}
        className={cn("flex items-start gap-2.5", !isBuild && "bg-accent")}
      >
        <ModeGlyph mode="ask" />
        <span className="flex flex-1 flex-col">
          <span className="text-sm font-medium">Ask</span>
          <span className="text-xs leading-snug text-muted-foreground">
            Answer questions and make quick workflow edits.
          </span>
        </span>
        {!isBuild ? <CheckIcon className="h-4 w-4 text-sky-400" /> : null}
      </DropdownMenuItem>
      <DropdownMenuItem
        aria-label="Build"
        onSelect={() => {
          setComposerMode("build");
          setCodeWorkflow(false);
          setCodeBlockRequestOverride(false);
        }}
        className={cn(
          "flex items-start gap-2.5",
          isBuild && !codeWorkflow && "bg-accent",
        )}
      >
        <ModeGlyph mode="build" />
        <span className="flex flex-1 flex-col">
          <span className="text-sm font-medium">Build</span>
          <span className="text-xs leading-snug text-muted-foreground">
            Navigates the site to design your workflow, then tests that it
            works.
          </span>
        </span>
        {isBuild && !codeWorkflow ? (
          <CheckIcon className="h-4 w-4 text-sky-400" />
        ) : null}
      </DropdownMenuItem>
      {codeOptionAvailable ? (
        <DropdownMenuItem
          aria-label={
            copilotUxV1Enabled ? "Build with code" : "Build workflow as code"
          }
          onSelect={() => {
            setComposerMode("build");
            setCodeWorkflow(true);
            setCodeBlockRequestOverride(true);
          }}
          className={cn(
            "flex items-start gap-2.5",
            isBuild && codeWorkflow && "bg-accent",
          )}
        >
          <ModeGlyph mode="build" glow />
          <span className="flex flex-1 flex-col">
            <span className="text-sm font-medium">
              {copilotUxV1Enabled
                ? "Build with code"
                : "Build workflow as code"}
            </span>
            <span className="text-xs leading-snug text-muted-foreground">
              Build the workflow as code. Faster and more flexible, but may need
              extra detail to handle every edge case.
            </span>
          </span>
          {isBuild && codeWorkflow ? (
            <CheckIcon className="h-4 w-4 text-sky-400" />
          ) : null}
        </DropdownMenuItem>
      ) : null}
    </>
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
              </div>
            ) : null}
            <ConvoAggregatePill
              messages={messages}
              isInFlight={
                isLoading ||
                (narrative.turnId !== null && narrative.terminal === null)
              }
            />
            {messages.map((message, index) => {
              const isLastMessage = index === lastTurnIndex;
              if (message.kind === "run_lifecycle") {
                return (
                  <RunLifecycleLine
                    key={message.id}
                    content={message.content}
                  />
                );
              }
              // Per-message frozen narrative. When an AI message carries a
              // frozen narrative, render the narrative card stack in place
              // of the legacy text bubble so the per-block cards survive
              // subsequent turns. The Accept/Reject controls render only on
              // the latest message AND while the proposal is pending review.
              if (message.sender === "ai" && message.narrative) {
                const turnId = message.narrative.turnId;
                const showProposalActions =
                  isLastMessage && Boolean(proposedWorkflow);
                const showReviewGate =
                  copilotUxV1Enabled &&
                  (shouldShowDiffCard(message.narrative) ||
                    (turnId !== null && turnId === pendingProposalTurnId));
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
                      uxV1={copilotUxV1Enabled}
                    />
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
                        gateId={turnId ? `copilot-gate-${turnId}` : undefined}
                        flash={turnId !== null && turnId === gateFlashTurnId}
                      />
                    ) : !copilotUxV1Enabled ? (
                      <>
                        {showProposalActions && proposedWorkflow ? (
                          <div className="flex flex-wrap gap-2 pl-1">
                            <button
                              type="button"
                              onClick={() =>
                                handleReviewWorkflow(proposedWorkflow)
                              }
                              className="rounded border border-cta/60 bg-cta/10 px-3 py-1 text-xs text-foreground hover:bg-cta/20"
                            >
                              Review
                            </button>
                            <button
                              type="button"
                              onClick={() =>
                                handleAcceptWorkflow(proposedWorkflow)
                              }
                              className="rounded bg-success px-3 py-1 text-xs text-success-foreground hover:bg-success/90"
                            >
                              Accept
                            </button>
                            <button
                              type="button"
                              onClick={() =>
                                handleAcceptWorkflow(proposedWorkflow, true)
                              }
                              className="rounded bg-success px-3 py-1 text-xs text-success-foreground hover:bg-success/80"
                            >
                              Always accept
                            </button>
                            <button
                              type="button"
                              onClick={handleRejectWorkflow}
                              className="rounded bg-destructive px-3 py-1 text-xs text-destructive-foreground hover:bg-destructive/90"
                            >
                              Reject
                            </button>
                          </div>
                        ) : null}
                        {docked && shouldShowDiffCard(message.narrative) ? (
                          <DiffCard
                            pendingProposal={showProposalActions}
                            rejected={
                              message.narrative.turnId !== null &&
                              rejectedTurnIds.has(message.narrative.turnId)
                            }
                            turn={message.narrative}
                          />
                        ) : null}
                      </>
                    ) : null}
                    {copilotUxV1Enabled &&
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
                    {docked &&
                    isLastMessage &&
                    shouldShowFixCard(message.narrative) ? (
                      <FixCard
                        turn={message.narrative}
                        onFix={() =>
                          handleSend(
                            "The last run failed — diagnose the failure and fix it, then re-run.",
                          )
                        }
                      />
                    ) : null}
                  </div>
                );
              }
              const showProposedPanel = isLastMessage && proposedWorkflow;
              const isGateOwnerOrLast =
                index === gateIndex && Boolean(proposedWorkflow);
              return (
                <MessageItem
                  key={message.id}
                  message={message}
                  queuedStatus={
                    copilotUxV1Enabled &&
                    copilotV2Enabled &&
                    queuedPrompt?.reason === "live_browser" &&
                    queuedPrompt.id === message.id
                      ? {
                          text: queuedPromptWaitingStatus,
                          onCancel: cancelQueuedPrompt,
                        }
                      : null
                  }
                  footer={
                    copilotUxV1Enabled && isGateOwnerOrLast ? (
                      <ReviewGateCard
                        pending
                        verdict={getReviewGateVerdict(
                          undefined,
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
                      />
                    ) : !copilotUxV1Enabled && showProposedPanel ? (
                      <>
                        <button
                          type="button"
                          onClick={() => handleReviewWorkflow(proposedWorkflow)}
                          className="rounded border border-cta/60 bg-cta/10 px-3 py-1 text-xs text-foreground hover:bg-cta/20"
                        >
                          Review
                        </button>
                        <button
                          type="button"
                          onClick={() => handleAcceptWorkflow(proposedWorkflow)}
                          className="rounded bg-success px-3 py-1 text-xs text-success-foreground hover:bg-success/90"
                        >
                          Accept
                        </button>
                        <button
                          type="button"
                          onClick={() =>
                            handleAcceptWorkflow(proposedWorkflow, true)
                          }
                          className="rounded bg-success px-3 py-1 text-xs text-success-foreground hover:bg-success/80"
                        >
                          Always accept
                        </button>
                        <button
                          type="button"
                          onClick={handleRejectWorkflow}
                          className="rounded bg-destructive px-3 py-1 text-xs text-destructive-foreground hover:bg-destructive/90"
                        >
                          Reject
                        </button>
                      </>
                    ) : null
                  }
                />
              );
            })}
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
                  uxV1={copilotUxV1Enabled}
                />
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
        {copilotUxV1Enabled && copilotV2Enabled ? (
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
                  <ModeGlyph
                    mode={isBuild ? "build" : "ask"}
                    glow={codeStateActive}
                  />
                  <span className="text-foreground">
                    {codeStateActive
                      ? "Build with code"
                      : isBuild
                        ? "Build"
                        : "Ask"}
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
        {copilotUxV1Enabled &&
        proposedWorkflow &&
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
        {copilotUxV1Enabled &&
        copilotV2Enabled &&
        queuedPrompt &&
        queuedPrompt.reason === "working" ? (
          <div className="mb-2 flex items-center gap-2 rounded-lg border border-border bg-slate-elevation2 px-2.5 py-1.5 text-xs text-muted-foreground">
            <ReloadIcon className="h-3 w-3 shrink-0 animate-spin" />
            <span className="shrink-0 font-medium text-foreground">Queued</span>
            <span className="flex-1 truncate">{queuedPromptWaitingStatus}</span>
            <button
              type="button"
              onClick={cancelQueuedPrompt}
              title="Cancel queued message"
              aria-label="Cancel queued message"
              className="shrink-0 rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
            >
              <Cross2Icon className="h-3 w-3" />
            </button>
          </div>
        ) : null}
        {inputStatusText ? (
          <div
            className="mb-2 text-xs text-muted-foreground"
            aria-live="polite"
          >
            {inputStatusText}
          </div>
        ) : null}
        <div className="flex items-end gap-2">
          <SpeechInputButton
            isSupported={isSpeechSupported}
            isListening={isSpeechListening}
            isHearingSpeech={isSpeechHearing}
            disabled={inputDisabled}
            onToggle={toggleSpeech}
            className={
              copilotUxV1Enabled && copilotV2Enabled
                ? "h-8 w-8 rounded-full border-0 bg-transparent"
                : "h-10 w-10 rounded-lg"
            }
            iconClassName={
              copilotUxV1Enabled && copilotV2Enabled ? "h-3.5 w-3.5" : undefined
            }
          />
          <textarea
            ref={setTextareaRef}
            placeholder={
              queuedPrompt
                ? "Prompt queued..."
                : isLoading
                  ? "Type a message to send next…"
                  : isWaitingForLiveBrowser
                    ? "Type a prompt to send when ready..."
                    : "Message Skyvern Copilot, or paste recorded steps…"
            }
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onPaste={() => dismissPasteSkillHint()}
            onKeyDown={handleKeyPress}
            disabled={inputDisabled}
            rows={1}
            className="min-h-10 flex-1 resize-none rounded-lg border border-input bg-slate-elevation2 px-3 py-2 text-sm leading-6 text-foreground placeholder:truncate placeholder:text-muted-foreground focus:border-ring focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
            style={{
              minHeight: "40px",
              maxHeight: "150px",
              overflowY: "hidden",
            }}
          />
          {copilotUxV1Enabled && copilotV2Enabled ? (
            <TooltipProvider>
              <ControlTooltip
                content={morphButtonLabel}
                blocked={waitingOnQueueOnly}
              >
                <button
                  type="button"
                  disabled={waitingOnQueueOnly}
                  onClick={() =>
                    isLoading && !hasComposerText ? cancelSend() : handleSend()
                  }
                  aria-label={morphButtonLabel}
                  className={cn(
                    "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50",
                    isLoading && !hasComposerText
                      ? "bg-slate-elevation4 text-foreground hover:bg-slate-elevation3"
                      : "bg-cta text-cta-foreground hover:bg-cta-hover",
                  )}
                >
                  {isLoading && !hasComposerText ? (
                    <StopIcon className="h-3 w-3" />
                  ) : (
                    <ArrowUpIcon className="h-4 w-4" />
                  )}
                </button>
              </ControlTooltip>
            </TooltipProvider>
          ) : isLoading && queuedPrompt ? (
            <>
              <button
                onClick={cancelQueuedPrompt}
                title="Edit queued message"
                className="flex h-10 items-center justify-center rounded-lg border border-border px-3 text-sm font-medium text-muted-foreground hover:bg-accent hover:text-accent-foreground"
              >
                Edit queued
              </button>
              <button
                onClick={cancelSend}
                title="Cancel run"
                className="flex h-10 items-center justify-center rounded-lg bg-destructive px-3 text-sm font-medium text-destructive-foreground hover:bg-destructive/90"
              >
                Cancel run
              </button>
            </>
          ) : isLoading ? (
            <>
              <button
                type="button"
                onClick={() => handleSend()}
                title="Send after this turn finishes"
                className="flex h-10 items-center justify-center rounded-lg border border-border px-3 text-sm font-medium text-muted-foreground hover:bg-accent hover:text-accent-foreground"
              >
                Send next
              </button>
              <button
                type="button"
                onClick={cancelSend}
                title="Cancel run"
                className="flex h-10 items-center justify-center rounded-lg bg-destructive px-3 text-sm font-medium text-destructive-foreground hover:bg-destructive/90"
              >
                Cancel run
              </button>
            </>
          ) : queuedPrompt ? (
            <button
              onClick={cancelQueuedPrompt}
              title="Edit queued prompt"
              className="flex h-10 items-center justify-center rounded-lg bg-destructive px-4 text-sm font-medium text-destructive-foreground hover:bg-destructive/90"
            >
              Cancel
            </button>
          ) : !copilotV2Enabled ? (
            <button
              onClick={() => handleSend()}
              className="flex h-10 items-center justify-center rounded-lg bg-cta px-4 text-sm font-medium text-cta-foreground hover:bg-cta-hover"
            >
              Send
            </button>
          ) : (
            <div className="flex items-stretch">
              <button
                onClick={() => handleSend()}
                className="flex h-10 items-center gap-2 rounded-l-lg bg-cta px-3 py-1.5 text-sm font-medium text-cta-foreground hover:bg-cta-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <ModeGlyph
                  mode={isBuild ? "build" : "ask"}
                  tone="dark"
                  glow={codeStateActive}
                />
                {codeStateActive ? (
                  <span className="flex flex-col items-start">
                    <span className="text-sm font-medium leading-tight">
                      Build
                    </span>
                    <span className="text-[10px] font-medium leading-tight text-cta-foreground/70">
                      with code
                    </span>
                  </span>
                ) : (
                  <span>{isBuild ? "Build" : "Ask"}</span>
                )}
              </button>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    type="button"
                    title="Switch mode"
                    aria-label="Switch mode"
                    className="flex h-10 w-8 items-center justify-center rounded-r-lg border-l border-black/20 bg-cta text-cta-foreground hover:bg-cta-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <ChevronDownIcon className="h-3.5 w-3.5" />
                  </button>
                </DropdownMenuTrigger>
                {/* onCloseAutoFocus: don't return focus to the caret on close,
                    so its focus ring doesn't linger after a click. */}
                <DropdownMenuContent
                  side="top"
                  align="end"
                  className="w-[272px] p-1.5"
                  onCloseAutoFocus={(event) => event.preventDefault()}
                >
                  {modeMenuItems}
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          )}
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
    </div>
  );

  if (docked) {
    return portalTarget ? createPortal(content, portalTarget) : null;
  }
  return content;
}
