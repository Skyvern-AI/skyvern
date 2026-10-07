import {
  Fragment,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  CheckIcon,
  ChevronRightIcon,
  CodeIcon,
  Cross2Icon,
  DotsHorizontalIcon,
  GlobeIcon,
  ListBulletIcon,
  LockClosedIcon,
  PlayIcon,
  ReaderIcon,
  StopIcon,
} from "@radix-ui/react-icons";
import {
  REVEAL_MS_PER_CHAR,
  buildRevealOffsets,
  revealedCharsAt,
  revealedCountAt,
} from "./actionReveal";
import { humanizeBlockLabel } from "./blockLabel";
import { fitSteps, STEP_SEPARATOR } from "./fitSteps";
import { CopilotMarkdown } from "./CopilotMarkdown";
import {
  ACTIVITY_KIND_WORD,
  ActivityLog,
  ActivityRow as ActivityRowModel,
  FinishedTurnSummary,
  blocksByRunCall,
  callLabel,
  callRollup,
  condenseCalls,
  deriveActivityLog,
  failedRowBlocks,
  passedBlockCount,
  rowIndexOfToolCall,
  summarizeFinishedTurn,
} from "./copilotActivityLog";
import { TURN_ROW_INSET, TURN_ROW_OUTSET } from "./cards/cardLayout";
import { showPhaseChecklist } from "./copilotPhases";
import { CodeWriteDiff } from "./workflowCopilotTypes";
import {
  ActivityEntry,
  BlockState,
  BudgetExpiryState,
  RecordedActionSummary,
  ToolCallKind,
  TurnNarrativeState,
  blockPassed,
  formatElapsed,
  humanizeJudgeText,
  hasObservedBlockEvidence,
  isBlockOk,
  isInterimOutcome,
  notConfirmedOutcome,
  parseUtcIsoMs,
  toolCallIdOf,
  terminalNarrativeText,
  toolActivityDisplayLabel,
  toolCallKind,
} from "./narrativeState";
import { useShimmerText } from "../workflowRun/useShimmerText";

// Row flashes green/red for 600ms once revealed — must match the tailwind
// copilot-row-flash-* animation duration.
const FLASH_WINDOW_MS = 600;
const MAX_DRAFTING_LABELS = 6;
const OUTCOME_REASON_PREVIEW_LIMIT = 140;
const OUTCOME_NOT_CONFIRMED_REASON =
  "the run finished without showing the goal was met";
const QUESTION_PROSE_CLASSES =
  "border-l-2 border-sky-500 pl-3 text-sky-700 dark:text-[#a7ccdd]";
const TERMINAL_PROSE_GRADIENT_CHARS = 32;
const TERMINAL_PROSE_GRADIENT_SETTLE_MS = 420;

function normalizeOutcomeReason(
  reason: string | null | undefined,
): string | null {
  const trimmed = reason?.trim();
  if (!trimmed) return null;
  const humanized = humanizeJudgeText(trimmed);
  return humanized.length > 0 ? humanized : null;
}

function truncateOutcomeReason(reason: string): string {
  if (reason.length <= OUTCOME_REASON_PREVIEW_LIMIT) return reason;
  const slice = reason.slice(0, OUTCOME_REASON_PREVIEW_LIMIT - 3).trimEnd();
  return `${slice}...`;
}

// What a collapsed line says about a block whose run did not confirm its
// outcome, or null when there is nothing to say.
function collapsedOutcomeNote(
  block: BlockState,
  ownsOutcomeNotConfirmed: boolean,
  outcomeReasonFallback: string | null | undefined,
): string | null {
  const completed = block.state === "completed";
  const notShown =
    completed &&
    block.outcome === "not_demonstrated" &&
    !isInterimOutcome(block.outcomeRole);
  const reason =
    notShown || ownsOutcomeNotConfirmed
      ? normalizeOutcomeReason(block.outcomeReason ?? outcomeReasonFallback)
      : null;
  if (notShown) {
    return `Outcome not confirmed — ${OUTCOME_NOT_CONFIRMED_REASON}${
      reason ? `: ${truncateOutcomeReason(reason)}` : "."
    }`;
  }
  if (ownsOutcomeNotConfirmed && reason !== null) {
    return `Outcome not confirmed — ${truncateOutcomeReason(reason)}`;
  }
  return null;
}

function notConfirmedDisplayReason(turn: TurnNarrativeState): string | null {
  return normalizeOutcomeReason(notConfirmedOutcome(turn)?.displayReason);
}

function isQuestionTurn(turn: TurnNarrativeState): boolean {
  return (
    !turn.cancelled &&
    (turn.responseType === "ASK_QUESTION" || turn.responseKind === "clarify")
  );
}

function blockIdentity(block: BlockState): string {
  return block.workflowRunBlockId || block.label;
}

function outcomeNotConfirmedOwnerKey(turn: TurnNarrativeState): string | null {
  if (notConfirmedOutcome(turn) === null) return null;

  for (let i = turn.blocks.length - 1; i >= 0; i -= 1) {
    const block = turn.blocks[i]!;
    if (
      block.state === "completed" &&
      block.outcome === "not_demonstrated" &&
      !isInterimOutcome(block.outcomeRole)
    ) {
      return blockIdentity(block);
    }
  }

  for (let i = turn.blocks.length - 1; i >= 0; i -= 1) {
    const block = turn.blocks[i]!;
    if (block.state === "failed" || block.state === "stopped") {
      return blockIdentity(block);
    }
  }

  return null;
}

interface BlockPalette {
  fg: string;
  bg: string;
  border: string;
  glyph: string;
}

const PALETTE_NAV: BlockPalette = {
  fg: "text-blue-700 dark:text-blue-300",
  bg: "bg-blue-500/15",
  border: "border-blue-400/60",
  glyph: "→",
};
const PALETTE_CRED: BlockPalette = {
  fg: "text-amber-700 dark:text-amber-300",
  bg: "bg-amber-500/15",
  border: "border-amber-400/60",
  glyph: "⌬",
};
const PALETTE_LOOP: BlockPalette = {
  fg: "text-sky-700 dark:text-sky-300",
  bg: "bg-sky-500/15",
  border: "border-sky-400/60",
  glyph: "↻",
};
const PALETTE_ACTION: BlockPalette = {
  fg: "text-emerald-700 dark:text-emerald-300",
  bg: "bg-emerald-500/15",
  border: "border-emerald-400/60",
  glyph: "✦",
};
const PALETTE_EXTRACTION: BlockPalette = {
  fg: "text-sky-700 dark:text-sky-300",
  bg: "bg-sky-500/15",
  border: "border-sky-400/60",
  glyph: "↓",
};
const PALETTE_TASK: BlockPalette = {
  fg: "text-tertiary-foreground",
  bg: "bg-slate-500/15",
  border: "border-slate-500/60",
  glyph: "✦",
};

function paletteFor(blockType: string): BlockPalette {
  const key = blockType.toLowerCase();
  if (key.includes("nav") || key.includes("goto") || key.includes("url")) {
    return PALETTE_NAV;
  }
  if (key.includes("cred") || key.includes("login")) return PALETTE_CRED;
  if (key.includes("loop") || key.includes("for_each")) return PALETTE_LOOP;
  if (key.includes("extract")) return PALETTE_EXTRACTION;
  if (
    key.includes("task") ||
    key.includes("action") ||
    key.includes("send") ||
    key.includes("email") ||
    key.includes("code")
  ) {
    return PALETTE_ACTION;
  }
  return PALETTE_TASK;
}

function liveElapsed(startedAt: string | null): string | null {
  const ms = parseUtcIsoMs(startedAt);
  if (ms === null) return null;
  const secs = Math.max(0, Math.round((Date.now() - ms) / 1000));
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function Spinner({ small = false }: { small?: boolean }) {
  const sizeClass = small ? "h-2 w-2" : "h-2.5 w-2.5";
  return (
    <span
      aria-hidden="true"
      className={`${sizeClass} inline-block animate-spin rounded-full border-[1.5px] border-blue-400/30 border-t-blue-400`}
    />
  );
}

function FProse({
  text,
  muted,
  italic,
}: {
  text: string;
  muted?: boolean;
  italic?: boolean;
}) {
  return (
    <div
      className={[
        "py-0.5 pl-9 pr-0 text-[13px] leading-[1.55]",
        muted ? "text-muted-foreground" : "text-foreground dark:text-slate-200",
        italic ? "italic" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {text}
    </div>
  );
}

function FSubRow({
  glyph,
  glyphClass,
  children,
  italic,
  muted,
}: {
  glyph: React.ReactNode;
  glyphClass?: string;
  children: React.ReactNode;
  italic?: boolean;
  muted?: boolean;
}) {
  return (
    <div className="flex items-start gap-2 py-px">
      <span
        className={`mt-[2px] flex w-3.5 shrink-0 justify-center text-[11px] font-bold ${glyphClass ?? "text-muted-foreground"}`}
        aria-hidden="true"
      >
        {glyph}
      </span>
      <div
        className={[
          "min-w-0 flex-1 text-[11.5px] leading-[1.55]",
          muted
            ? "text-muted-foreground"
            : "text-foreground dark:text-slate-200",
          italic ? "italic" : "",
        ]
          .filter(Boolean)
          .join(" ")}
      >
        {children}
      </div>
    </div>
  );
}

function AttemptsBadge({ attempts }: { attempts?: number }) {
  if (!attempts || attempts <= 1) return null;
  return (
    <span className="text-muted-foreground dark:text-slate-500">
      {" "}
      · ↻ {attempts} attempts
    </span>
  );
}

function ActivityRow({ entry }: { entry: ActivityEntry }) {
  return (
    <div data-tool-call-id={toolCallIdOf(entry)}>
      {entry.reason === undefined ? null : (
        <p
          data-testid="copilot-reason"
          className="mb-1 whitespace-pre-wrap text-[12px] leading-[1.5]"
        >
          {entry.reason}
        </p>
      )}
      <ActivityActionRow entry={entry} />
    </div>
  );
}

function ActivityActionRow({ entry }: { entry: ActivityEntry }) {
  if (entry.kind === "narration") {
    return (
      <FSubRow
        glyph="✦"
        glyphClass="text-sky-700 dark:text-sky-300"
        italic
        muted
      >
        {entry.text}
      </FSubRow>
    );
  }
  if (entry.kind === "tool_call") {
    const label =
      entry.displayLabel ?? toolActivityDisplayLabel(entry.toolName);
    return (
      <FSubRow glyph="▸" glyphClass="text-muted-foreground">
        <span className="text-foreground dark:text-slate-200">{label}</span>
        <span className="text-muted-foreground dark:text-slate-500">
          {" "}
          · calling…
        </span>
        <AttemptsBadge attempts={entry.attempts} />
      </FSubRow>
    );
  }
  const ok = entry.success !== false;
  return (
    <FSubRow
      glyph={ok ? "✓" : "✕"}
      glyphClass={
        ok
          ? "text-emerald-700 dark:text-emerald-300"
          : "text-rose-700 dark:text-rose-300"
      }
    >
      <span
        className={
          ok
            ? "text-foreground dark:text-slate-200"
            : "text-rose-700 dark:text-rose-200"
        }
      >
        {entry.text}
      </span>
      <AttemptsBadge attempts={entry.attempts} />
    </FSubRow>
  );
}

function useTick(active: boolean, intervalMs = 1000): void {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setTick((t) => t + 1), intervalMs);
    return () => clearInterval(id);
  }, [active, intervalMs]);
}

// Both reveals advance faster than an interval coarse enough for status text:
// narration moves a character every REVEAL_MS_PER_CHAR, and buildRevealOffsets scales a long
// block's steps under 150ms, so a timer samples them in visible jumps.
function useFrameTick(active: boolean): void {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!active) return;
    let raf = requestAnimationFrame(function loop() {
      setTick((t) => t + 1);
      raf = requestAnimationFrame(loop);
    });
    return () => cancelAnimationFrame(raf);
  }, [active]);
}

function FRecordedActionRow({
  action,
  revealing,
  flash,
}: {
  action: RecordedActionSummary;
  revealing: boolean;
  flash: boolean;
}) {
  const shimmerRef = useShimmerText<HTMLSpanElement>(revealing);
  if (revealing) {
    return (
      <FSubRow
        glyph={<Spinner small />}
        glyphClass="text-blue-700 dark:text-blue-300"
      >
        <span ref={shimmerRef} className="text-foreground dark:text-slate-200">
          {action.label}
        </span>
        {action.summary ? (
          <span className="text-muted-foreground dark:text-slate-500">
            {" "}
            · {action.summary}
          </span>
        ) : null}
      </FSubRow>
    );
  }
  const flashClass = flash
    ? action.failed
      ? "animate-copilot-row-flash-error"
      : "animate-copilot-row-flash-success"
    : "";
  const detail = [
    action.codeLine !== null ? `line ${action.codeLine}` : action.summary,
    action.response,
  ].filter((part): part is string => Boolean(part));
  return (
    <FSubRow
      glyph={action.failed ? "✕" : "✓"}
      glyphClass={
        action.failed
          ? "text-rose-700 dark:text-rose-300"
          : "text-emerald-700 dark:text-emerald-300"
      }
    >
      <span
        className={`${action.failed ? "text-rose-700 dark:text-rose-200" : "text-foreground dark:text-slate-200"} ${flashClass}`}
      >
        {action.label}
      </span>
      {detail.length > 0 ? (
        <span className="text-muted-foreground dark:text-slate-500">
          {" "}
          · {detail.join(SEP)}
        </span>
      ) : null}
    </FSubRow>
  );
}

interface ActionReveal {
  // Recorded actions shown so far, oldest first.
  visible: RecordedActionSummary[];
  total: number;
  // Index into `visible` of the action still replaying, or -1.
  revealingIndex: number;
  flashing: (index: number) => boolean;
}

// Time-derived, not timer-chained: recomputed from wall-clock time on every
// render/tick so collapse, remount, and StrictMode double-invoke can never
// restart or duplicate the reveal.
function useActionReveal(block: BlockState): ActionReveal {
  const recordedActions = block.recordedActions;
  const total = recordedActions?.length ?? 0;
  const durations = useMemo(
    () => (recordedActions ?? []).map((a) => a.durationMs),
    [recordedActions],
  );
  const offsets = useMemo(() => buildRevealOffsets(durations), [durations]);
  const totalMs = offsets.length > 0 ? offsets[offsets.length - 1]! : 0;
  const elapsedReveal =
    total > 0 ? Date.now() - (block.recordedActionsAt ?? 0) : 0;
  const revealedCount = total > 0 ? revealedCountAt(offsets, elapsedReveal) : 0;
  const replaying = total > 0 && elapsedReveal >= 0 && revealedCount < total;
  const visibleCount =
    total === 0 || elapsedReveal < 0
      ? 0
      : Math.min(revealedCount + (replaying ? 1 : 0), total);
  useFrameTick(total > 0 && (replaying || elapsedReveal < totalMs));
  return {
    visible: (recordedActions ?? []).slice(0, visibleCount),
    total,
    revealingIndex: replaying ? revealedCount : -1,
    flashing: (i) =>
      i < revealedCount && elapsedReveal - offsets[i]! < FLASH_WINDOW_MS,
  };
}

interface FBlockRunProps {
  block: BlockState;
  turnEnded: boolean;
  onSelect?: (label: string) => void;
  outcomeReasonFallback?: string | null;
  ownsOutcomeNotConfirmed?: boolean;
}

function FBlockRun({
  block,
  turnEnded,
  onSelect,
  outcomeReasonFallback,
  ownsOutcomeNotConfirmed,
}: FBlockRunProps) {
  const displayLabel = humanizeBlockLabel(block.label);
  const palette = paletteFor(block.blockType);
  const isRunning = block.state === "running";
  const isCompleted = block.state === "completed";
  const isEvaluating = isCompleted && block.outcome === "evaluating";
  const isInterimNotDemonstrated =
    isCompleted &&
    block.outcome === "not_demonstrated" &&
    isInterimOutcome(block.outcomeRole);
  // A row stuck in `evaluating` at turn end (dropped stream) renders the
  // neutral "ran" treatment — never the live verifying beat, never green.
  const isVerifying = isEvaluating && !turnEnded;
  const isRanNeutral = (isEvaluating && turnEnded) || isInterimNotDemonstrated;
  const isOutcomeNotShown =
    isCompleted &&
    block.outcome === "not_demonstrated" &&
    !isInterimNotDemonstrated;
  const isOk = isBlockOk(block);
  const isFail = block.state === "failed";
  const isStopped = block.state === "stopped";
  const isDraft = block.state === "drafted";
  const collapsedOutcomeReason =
    isOutcomeNotShown || ownsOutcomeNotConfirmed
      ? normalizeOutcomeReason(block.outcomeReason ?? outcomeReasonFallback)
      : null;
  const ownsOutcomeReason =
    ownsOutcomeNotConfirmed === true && collapsedOutcomeReason !== null;

  const accentBorder = isRunning
    ? "border-blue-400/60"
    : isOk
      ? "border-emerald-400/60"
      : isOutcomeNotShown
        ? "border-amber-400/60"
        : isFail
          ? "border-rose-400/60"
          : "border-slate-500/60";
  const accentText = isRunning
    ? "text-blue-700 dark:text-blue-300"
    : isOk
      ? "text-emerald-700 dark:text-emerald-300"
      : isOutcomeNotShown
        ? "text-amber-700 dark:text-amber-300"
        : isFail
          ? "text-rose-700 dark:text-rose-300"
          : isVerifying || isRanNeutral
            ? "text-tertiary-foreground"
            : "text-muted-foreground";
  const puckBg = isRunning
    ? "bg-blue-500/15"
    : isOk
      ? "bg-emerald-500/15"
      : isOutcomeNotShown
        ? "bg-amber-500/15"
        : isFail
          ? "bg-rose-500/15"
          : "bg-slate-elevation3";

  const reveal = useActionReveal(block);
  const hasActions = reveal.total > 0;

  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const defaultOpen = isRunning || isFail || (hasActions && !turnEnded);
  const open = userOpen === null ? defaultOpen : userOpen;
  // A stop stays inspectable but not self-opening: the user knows why it
  // stopped, so it should not demand attention the way a failure does.
  const hasExpandableDetail =
    isRunning ||
    block.activity.length > 0 ||
    hasActions ||
    isFail ||
    isOutcomeNotShown ||
    ownsOutcomeReason;
  const toggleable =
    hasExpandableDetail &&
    (isOk ||
      isOutcomeNotShown ||
      ownsOutcomeReason ||
      isVerifying ||
      isRanNeutral ||
      isStopped);
  useTick(isRunning);
  const elapsed = formatElapsed(block.startedAt, block.endedAt);
  const live = isRunning ? liveElapsed(block.startedAt) : null;
  const statusText = isOk
    ? (elapsed ?? "done")
    : isRunning
      ? `working${live ? ` · ${live}` : ""}`
      : isVerifying
        ? "ran · verifying outcome…"
        : isRanNeutral || isOutcomeNotShown
          ? `ran${elapsed ? ` · ${elapsed}` : ""}`
          : isFail
            ? "halted"
            : isStopped
              ? `stopped${elapsed ? ` · ${elapsed}` : ""}`
              : isDraft
                ? "drafted"
                : "queued";
  const stateGlyph = isOk ? (
    "✓"
  ) : isOutcomeNotShown ? (
    "!"
  ) : isVerifying ? (
    "…"
  ) : isFail ? (
    "✕"
  ) : isStopped ? (
    "■"
  ) : isRunning ? (
    <Spinner />
  ) : (
    palette.glyph
  );
  const failureActivity = block.activity.find(
    (entry) => entry.kind === "tool_result",
  )?.text;
  const failureDetail =
    failureActivity ?? collapsedOutcomeReason ?? "Halted — see run details.";
  // Keep the amber evidence box only when it adds information beyond the
  // failed row's rose failure box.
  const showSeparateOutcomeReason =
    ownsOutcomeReason && (!isFail || failureDetail !== collapsedOutcomeReason);

  const onHeaderClick = () => {
    onSelect?.(block.label);
    if (!toggleable) return;
    setUserOpen((v) => !(v === null ? defaultOpen : v));
  };
  const collapsedExtras = (
    <>
      {!open && isOk && block.activity.length > 0 ? (
        <div className="mt-0.5 text-[12px] leading-[1.5] text-muted-foreground">
          {block.activity[block.activity.length - 1]!.text}
        </div>
      ) : null}
      {!open && isOutcomeNotShown ? (
        <div className="mt-0.5 text-[12px] leading-[1.5] text-amber-700 dark:text-amber-200/80">
          {`Outcome not confirmed — ${OUTCOME_NOT_CONFIRMED_REASON}`}
          {collapsedOutcomeReason
            ? `: ${truncateOutcomeReason(collapsedOutcomeReason)}`
            : "."}
        </div>
      ) : null}
      {!open && ownsOutcomeReason && !isOutcomeNotShown ? (
        <div className="mt-0.5 text-[12px] leading-[1.5] text-amber-700 dark:text-amber-200/80">
          Outcome not confirmed —{" "}
          {truncateOutcomeReason(collapsedOutcomeReason)}
        </div>
      ) : null}
    </>
  );

  const blockDetail = (
    <div className="flex flex-col gap-1.5 border-l border-border/60 py-1.5 pl-3">
      {isRunning ? (
        <span className="inline-flex w-fit items-center gap-1.5 rounded-full border border-blue-400/40 bg-blue-500/10 px-2 py-0.5 text-[11px] font-semibold text-blue-700 dark:text-blue-300">
          <span className="h-[5px] w-[5px] animate-pulse rounded-full bg-blue-400" />
          Active in Live Browser
        </span>
      ) : null}
      {block.activity.length === 0 && isRunning ? (
        <FSubRow
          glyph={<Spinner small />}
          glyphClass="text-blue-700 dark:text-blue-300"
        >
          <span className="text-muted-foreground">Working…</span>
        </FSubRow>
      ) : null}
      {block.activity.map((entry) => (
        <ActivityRow key={entry.id} entry={entry} />
      ))}
      {reveal.visible.map((action, i) => (
        <FRecordedActionRow
          key={action.actionId}
          action={action}
          revealing={i === reveal.revealingIndex}
          flash={reveal.flashing(i)}
        />
      ))}
      {isFail ? (
        <div className="mt-1 flex items-start gap-2 rounded-md border border-rose-400/30 bg-rose-500/10 px-2.5 py-1.5">
          <span className="text-[11px] font-bold text-rose-700 dark:text-rose-300">
            ✕
          </span>
          <div className="text-[12px] leading-[1.5] text-rose-700 dark:text-rose-200/90">
            {failureDetail}
          </div>
        </div>
      ) : null}
      {isOutcomeNotShown || showSeparateOutcomeReason ? (
        <div className="mt-1 flex items-start gap-2 rounded-md border border-amber-400/30 bg-amber-500/10 px-2.5 py-1.5">
          <span
            aria-hidden="true"
            className="text-[11px] font-bold text-amber-700 dark:text-amber-300"
          >
            !
          </span>
          <div className="text-[12px] leading-[1.5] text-amber-700 dark:text-amber-200/90">
            {collapsedOutcomeReason ??
              "The step ran, but the run did not demonstrate the goal was met."}
          </div>
        </div>
      ) : null}
    </div>
  );

  return (
    <div className="flex flex-col">
      <button
        type="button"
        className={`flex w-full items-start gap-3 px-1 py-1 text-left ${
          toggleable ? "cursor-pointer" : "cursor-default"
        }`}
        aria-expanded={toggleable ? open : undefined}
        onClick={onHeaderClick}
        title={`Highlight ${block.label} on canvas`}
      >
        <span
          className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-[11px] font-bold ${accentBorder} ${accentText} ${puckBg}`}
          aria-hidden="true"
        >
          {stateGlyph}
        </span>
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5">
            <span
              className="text-[12.5px] font-semibold text-foreground"
              title={block.label}
            >
              {displayLabel}
            </span>
            <span className="text-[11px] text-muted-foreground dark:text-slate-500">
              ·
            </span>
            <span className={`font-mono text-[11px] font-medium ${accentText}`}>
              {statusText}
            </span>
            <span className="text-[10.5px] text-muted-foreground dark:text-slate-500">
              · {block.blockType}
            </span>
          </div>
          {collapsedExtras}
        </div>
        {toggleable ? (
          <span
            className={`shrink-0 text-[12px] text-muted-foreground transition-transform dark:text-slate-500 ${
              open ? "rotate-90" : ""
            }`}
            aria-hidden="true"
          >
            ›
          </span>
        ) : null}
      </button>

      {open && hasExpandableDetail ? (
        <div className="ml-9">{blockDetail}</div>
      ) : null}
    </div>
  );
}

interface FDesignRowProps {
  done: boolean;
  blockLabels: string[];
  activity: ActivityEntry[];
}

function FDesignRow({ done, blockLabels, activity }: FDesignRowProps) {
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const open = userOpen === null ? !done : userOpen;
  const drafts = blockLabels.length;
  const thoughts = activity.filter(
    (e) => e.kind === "narration" || e.kind === "tool_call",
  ).length;
  const summary: string[] = [];
  if (thoughts) {
    summary.push(`${thoughts} thought${thoughts === 1 ? "" : "s"}`);
  }
  if (drafts) {
    summary.push(`drafted ${drafts} block${drafts === 1 ? "" : "s"}`);
  }
  const title = done ? "Designed the workflow" : "Designing the workflow";

  return (
    <div className="flex flex-col">
      <button
        type="button"
        className="flex w-full items-center gap-3 px-1 py-1 text-left"
        onClick={() => setUserOpen((v) => !(v === null ? !done : v))}
      >
        <span
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-sky-400/60 bg-sky-500/15 text-[11px] font-bold text-sky-700 dark:text-sky-300"
          aria-hidden="true"
        >
          {done ? "✓" : <Spinner />}
        </span>
        <div className="flex flex-1 items-baseline gap-2 text-left">
          <span className="text-[12.5px] font-semibold text-foreground">
            {title}
          </span>
          {summary.length ? (
            <span className="text-[11px] text-muted-foreground">
              · {summary.join(" · ")}
            </span>
          ) : null}
          {!done ? (
            <span className="text-[10.5px] uppercase tracking-wide text-blue-700 dark:text-blue-300">
              live
            </span>
          ) : null}
        </div>
        <span
          className={`shrink-0 text-[12px] text-muted-foreground transition-transform dark:text-slate-500 ${
            open ? "rotate-90" : ""
          }`}
          aria-hidden="true"
        >
          ›
        </span>
      </button>
      {open ? (
        <div className="ml-9 flex flex-col gap-1 border-l border-border/60 py-1.5 pl-3">
          {activity.map((entry) => (
            <ActivityRow key={entry.id} entry={entry} />
          ))}
          {blockLabels.map((label) => (
            <FSubRow
              key={label}
              glyph="✦"
              glyphClass="text-emerald-700 dark:text-emerald-300"
            >
              <span className="text-muted-foreground">Drafted </span>
              <span className="text-foreground" title={label}>
                {humanizeBlockLabel(label)}
              </span>
            </FSubRow>
          ))}
        </div>
      ) : null}
    </div>
  );
}

// Fills the send→first-event gap with one quiet line; the first step replaces it.
export function InstantAckPlaceholder() {
  return (
    <p
      role="status"
      className="mt-2 text-[13px] leading-[1.55] text-muted-foreground"
    >
      <span className="sr-only">Copilot is working on your request…</span>
      <span aria-hidden="true">Working…</span>
    </p>
  );
}

interface FActivityLogProps {
  turn: TurnNarrativeState;
  log: ActivityLog;
  turnEnded: boolean;
  onBlockSelect?: (label: string) => void;
  interactionRef?: { current: string | null };
  anchoredAfterRow?: ReadonlyMap<string, AnchoredTurnItem[]>;
  anchoredBeforeRows?: AnchoredTurnItem[];
}

// Unified-diff lines keep their +/- colors; a muted peek greys them out.
function renderPatchLine(line: string, i: number, muted: boolean) {
  return (
    <span
      key={`${i}-${line}`}
      className={[
        "block",
        line.startsWith("+")
          ? "text-emerald-700 dark:text-emerald-300"
          : line.startsWith("-")
            ? "text-rose-700 dark:text-rose-300"
            : "text-muted-foreground",
        muted ? "!text-muted-foreground" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {line}
    </span>
  );
}

function DiffPatch({
  patch,
  className,
}: {
  patch: string;
  className?: string;
}) {
  return (
    <pre
      className={`overflow-x-auto whitespace-pre rounded border border-border/60 bg-muted/40 p-2 text-[11px] leading-[1.5] ${className ?? ""}`}
    >
      {patch.split("\n").map((line, i) => renderPatchLine(line, i, false))}
    </pre>
  );
}

// The kind gutter sits to the left of ActivityRow's own status column, so a
// row reads <kind> <status> <text> and neither signal displaces the other.
// The counts line sits outside the row's own expand button, so its `view diff`
// control is never a button nested inside another button.
function FCodeWriteDiff({
  diff,
  open,
  peek,
  onToggle,
}: {
  diff: CodeWriteDiff;
  open: boolean;
  peek: boolean;
  onToggle: () => void;
}) {
  const expandedToggleRef = useRef<HTMLButtonElement>(null);
  const restoreFocusAfterPeek = useRef(false);
  useEffect(() => {
    if (!open || !restoreFocusAfterPeek.current) return;
    expandedToggleRef.current?.focus();
    restoreFocusAfterPeek.current = false;
  }, [open]);
  const patchLines = diff.patch?.split("\n") ?? [];
  return (
    <div className="flex flex-col">
      <FSubRow glyph="±" glyphClass="text-sky-700 dark:text-sky-300">
        <span className="text-foreground" title={diff.label}>
          {humanizeBlockLabel(diff.label)}
        </span>
        <span className="pl-1.5 font-mono text-emerald-700 dark:text-emerald-300">
          {`+${diff.added}`}
        </span>
        <span className="pl-1 font-mono text-rose-700 dark:text-rose-300">
          {`−${diff.removed}`}
        </span>
        {peek ? null : (
          /* A disabled control receives no pointer events, so the reason why it is
             dead has to hang on something that does. */
          <span
            title={
              diff.patchDropped
                ? "The diff was too large to keep, so only its line counts were saved."
                : undefined
            }
          >
            <button
              ref={expandedToggleRef}
              type="button"
              className="pl-2 text-muted-foreground underline-offset-2 hover:underline disabled:no-underline disabled:opacity-50"
              disabled={diff.patch === undefined}
              aria-expanded={open}
              onClick={onToggle}
            >
              {open ? "hide diff" : "view diff"}
            </button>
          </span>
        )}
      </FSubRow>
      {open && diff.patch !== undefined ? (
        <DiffPatch patch={diff.patch} className="ml-5" />
      ) : peek && diff.patch !== undefined ? (
        <button
          type="button"
          data-code-diff-peek="true"
          aria-label={`Expand code changes for ${diff.label}`}
          aria-expanded={false}
          className="relative ml-5 max-h-[72px] cursor-pointer overflow-hidden rounded bg-muted/20 px-2 pb-6 pt-2 text-left text-[11px] leading-[1.5] transition-colors hover:bg-muted/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring motion-reduce:transition-none"
          onClick={() => {
            restoreFocusAfterPeek.current = true;
            onToggle();
          }}
        >
          <code className="block whitespace-pre">
            {patchLines
              .slice(0, 2)
              .map((line, i) => renderPatchLine(line, i, true))}
          </code>
          <span
            aria-hidden="true"
            className="pointer-events-none absolute inset-x-0 bottom-0 h-5 bg-gradient-to-b from-transparent to-muted/80"
          />
        </button>
      ) : null}
    </div>
  );
}

const CALL_KIND_ICON: Record<ToolCallKind, typeof GlobeIcon> = {
  browser: GlobeIcon,
  credential: LockClosedIcon,
  plan: ListBulletIcon,
  guidance: ReaderIcon,
  write: CodeIcon,
  run: PlayIcon,
  other: DotsHorizontalIcon,
};

const STEP_TEXT = "text-foreground/75";
// The no-break space binds each "·" to the item before it, so a wrapped tail never starts a line with one.
const SEP = "\u00a0· ";

function Chevron({
  open,
  className = "text-muted-foreground",
}: {
  open: boolean;
  className?: string;
}) {
  return (
    <ChevronRightIcon
      aria-hidden="true"
      className={`size-3 shrink-0 transition-transform motion-reduce:transition-none ${className} ${
        open ? "rotate-90" : ""
      }`}
    />
  );
}

function FDetailBox({ children }: { children: React.ReactNode }) {
  return (
    <div className="mb-1.5 ml-[18px] mt-[3px] flex flex-col gap-1 rounded-lg border border-border bg-muted/40 px-[9px] py-[7px]">
      {children}
    </div>
  );
}

function DiffCounts({ added, removed }: { added: number; removed: number }) {
  return (
    <span className="whitespace-nowrap font-mono text-[11px] tabular-nums text-muted-foreground">
      {`+${added}`}
      <span className="pl-1">{`−${removed}`}</span>
    </span>
  );
}

// A failed block stays named on the collapsed line, in the neutral timeline
// palette; its exact error keeps the rose treatment inside the expansion.
function FFailurePin({ block }: { block: BlockState }) {
  return (
    <span
      className={`inline-flex items-center gap-1 whitespace-nowrap ${STEP_TEXT}`}
    >
      <Cross2Icon aria-hidden="true" className="size-3 text-muted-foreground" />
      <span title={block.label}>{humanizeBlockLabel(block.label)}</span>
      {/* A flex gap is not text, so copied or spoken text needs the space. */}{" "}
      <span className="text-muted-foreground">failed</span>
    </span>
  );
}

type TestCardTone =
  | "running"
  | "failed"
  | "notConfirmed"
  | "passed"
  | "neutral";

const TEST_CARD_TONE: Record<
  TestCardTone,
  { card: string; word: string; glyph: string }
> = {
  running: {
    card: "border-blue-400/40 bg-blue-400/[0.06]",
    word: "text-blue-700 dark:text-blue-300",
    glyph: "text-blue-600 dark:text-blue-400",
  },
  failed: {
    card: "border-rose-500/40 bg-rose-500/[0.06]",
    word: "text-rose-700 dark:text-rose-300",
    glyph: "text-rose-600 dark:text-rose-500",
  },
  notConfirmed: {
    card: "border-amber-500/40 bg-amber-500/[0.06]",
    word: "text-amber-700 dark:text-amber-300",
    glyph: "text-amber-600 dark:text-amber-300",
  },
  passed: {
    card: "border-emerald-500/40 bg-emerald-500/[0.06]",
    word: "text-emerald-700 dark:text-emerald-300",
    glyph: "text-emerald-600 dark:text-emerald-500",
  },
  neutral: {
    card: "border-slate-500/40 bg-slate-500/[0.06]",
    word: "text-slate-700 dark:text-slate-300",
    glyph: "text-slate-500 dark:text-slate-400",
  },
};

interface TestCardStatus {
  tone: TestCardTone;
  word: string;
  // Muted words after the status, for a state the word alone does not name.
  note: string | null;
  glyph: React.ReactNode;
}

function testCardStatus(block: BlockState, turnEnded: boolean): TestCardStatus {
  const glyphClass = "size-3.5";
  switch (block.state) {
    case "running":
      return {
        tone: "running",
        word: "Testing",
        note: null,
        glyph: <Spinner />,
      };
    case "failed":
      return {
        tone: "failed",
        word: "Failed",
        note: null,
        glyph: <Cross2Icon aria-hidden="true" className={glyphClass} />,
      };
    case "stopped":
      return {
        tone: "neutral",
        word: "Stopped",
        note: null,
        glyph: <StopIcon aria-hidden="true" className="size-3" />,
      };
    case "skipped":
      return { tone: "neutral", word: "Skipped", note: null, glyph: "–" };
    case "queued":
      return { tone: "neutral", word: "Queued", note: null, glyph: "·" };
    case "drafted":
      return { tone: "neutral", word: "Drafted", note: null, glyph: "·" };
    case "completed":
      break;
  }
  if (blockPassed(block)) {
    return {
      tone: "passed",
      word: "Passed",
      note: null,
      glyph: <CheckIcon aria-hidden="true" className={glyphClass} />,
    };
  }
  if (
    block.outcome === "not_demonstrated" &&
    !isInterimOutcome(block.outcomeRole)
  ) {
    return {
      tone: "notConfirmed",
      word: "Not confirmed",
      note: null,
      glyph: "!",
    };
  }
  // A verdict still pending at turn end (dropped stream) reads as ran, never
  // as the live verifying beat.
  if (block.outcome === "evaluating" && !turnEnded) {
    return {
      tone: "neutral",
      word: "Ran",
      note: "verifying outcome…",
      glyph: "…",
    };
  }
  return { tone: "neutral", word: "Ran", note: null, glyph: "•" };
}

function formatActionDuration(ms: number | null): string | null {
  if (ms === null) return null;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const secs = Math.round(ms / 1000);
  return `${Math.floor(secs / 60)}:${(secs % 60).toString().padStart(2, "0")}`;
}

// A failure's first line reads under its row in full; the rest of the recorded
// text opens below it. The split is display-only: no meaning is read from it.
function FFailureText({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const [firstLine = "", ...rest] = text.split("\n");
  const more = rest.join("\n").trim();
  const hasMore = more.length > 0;
  return (
    <div className="ml-[22px] flex flex-col gap-1.5">
      <p className="whitespace-pre-wrap break-words text-[12.5px] leading-[1.5] text-rose-800 [overflow-wrap:anywhere] dark:text-rose-100">
        {firstLine}
      </p>
      {hasMore && open ? (
        <pre className="whitespace-pre-wrap break-words rounded-md border border-rose-500/30 bg-background p-2 font-mono text-[11px] leading-[1.5] text-foreground/80 [overflow-wrap:anywhere]">
          {more}
        </pre>
      ) : null}
      {hasMore ? (
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          className="flex w-fit items-center gap-1 text-[11.5px] text-muted-foreground hover:text-foreground"
        >
          <Chevron open={open} />
          {open ? "Hide details" : "Show details"}
        </button>
      ) : null}
    </div>
  );
}

function FCardActionRow({
  action,
  revealing,
  flash,
  failureText,
}: {
  action: RecordedActionSummary;
  revealing: boolean;
  flash: boolean;
  failureText: string | null;
}) {
  const shimmerRef = useShimmerText<HTMLSpanElement>(revealing);
  const failed = !revealing && action.failed;
  const duration = revealing ? null : formatActionDuration(action.durationMs);
  const flashClass = flash
    ? failed
      ? "animate-copilot-row-flash-error"
      : "animate-copilot-row-flash-success"
    : "";
  const row = (
    <div className="flex items-start gap-2 text-[12px] leading-[1.5]">
      <span className="flex h-[18px] w-3.5 shrink-0 items-center justify-center">
        {revealing ? (
          <Spinner small />
        ) : failed ? (
          <Cross2Icon
            aria-hidden="true"
            className="size-[13px] text-rose-600 dark:text-rose-500"
          />
        ) : (
          <CheckIcon
            aria-hidden="true"
            className="size-[13px] text-emerald-600 dark:text-emerald-500"
          />
        )}
      </span>
      <span
        ref={shimmerRef}
        className={`shrink-0 ${
          failed
            ? "font-semibold text-rose-700 dark:text-rose-300"
            : "text-foreground"
        } ${flashClass}`}
      >
        {action.label}
      </span>
      <span
        className={`min-w-0 flex-1 break-words [overflow-wrap:anywhere] ${
          failed && action.codeLine !== null
            ? "text-rose-700 dark:text-rose-300"
            : "text-muted-foreground"
        }`}
      >
        {failed && action.codeLine !== null
          ? `line ${action.codeLine}`
          : action.summary}
      </span>
      {duration === null ? null : (
        <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground">
          {duration}
        </span>
      )}
      <span className="sr-only">{failed ? " · failed" : ""}</span>
    </div>
  );
  if (!failed) return row;
  return (
    <div className="-mx-1.5 flex flex-col gap-1.5 rounded-md bg-rose-500/[0.08] px-1.5 py-2">
      {row}
      {failureText === null ? null : <FFailureText text={failureText} />}
    </div>
  );
}

// One card per block run of a test. The header says where the block stands;
// the body holds the code change that block got and the steps its run took.
function FTestRunCard({
  block,
  diff,
  diffOpen,
  onDiffToggle,
  callFailure,
  outcomeReason,
  ownsVerdict,
  turnEnded,
  open,
  onToggle,
  onSelect,
}: {
  block: BlockState;
  diff: CodeWriteDiff | null;
  diffOpen: boolean;
  onDiffToggle: () => void;
  // The failed run call's error, for a failure that recorded no text of its own.
  callFailure: string | null;
  // Why the run did not confirm the block's outcome, when it says.
  outcomeReason: string | null;
  // This block holds the turn's unconfirmed verdict, so its card is where the
  // reason reads; the turn card stands down for it.
  ownsVerdict: boolean;
  turnEnded: boolean;
  open: boolean;
  onToggle: () => void;
  onSelect?: (label: string) => void;
}) {
  const status = testCardStatus(block, turnEnded);
  const tone = TEST_CARD_TONE[status.tone];
  const isRunning = block.state === "running";
  useTick(isRunning);
  const reveal = useActionReveal(block);
  const elapsed = isRunning
    ? liveElapsed(block.startedAt)
    : formatElapsed(block.startedAt, block.endedAt);

  const actions = reveal.visible;
  let lastFailed = -1;
  actions.forEach((action, i) => {
    if (action.failed && i !== reveal.revealingIndex) lastFailed = i;
  });
  const failureOf = (action: RecordedActionSummary, i: number) =>
    action.response ?? (i === lastFailed ? callFailure : null);
  // A failed block whose run recorded no failed step still says why. It stays
  // put while actions replay above it, then moves onto the step that failed.
  const recordedFailure = (block.recordedActions ?? []).some((a) => a.failed);
  const blockFailure =
    block.state === "failed" && lastFailed === -1
      ? (callFailure ?? (recordedFailure ? null : "Halted — see run details."))
      : null;
  const reason =
    status.tone === "notConfirmed"
      ? (outcomeReason ??
        "The step ran, but the run did not demonstrate the goal was met.")
      : ownsVerdict && outcomeReason !== null
        ? `Outcome not confirmed — ${outcomeReason}`
        : null;

  const hasBody =
    diff !== null ||
    actions.length > 0 ||
    blockFailure !== null ||
    block.activity.length > 0;
  const expanded = hasBody && open;
  const summary = [
    reveal.total > 0
      ? `${reveal.total} ${reveal.total === 1 ? "action" : "actions"}`
      : null,
    elapsed,
  ]
    .filter((part) => part !== null)
    .join(" · ");

  const header = (
    <>
      <span
        className={`flex h-5 w-3.5 shrink-0 items-center justify-center text-[12px] font-bold ${tone.glyph}`}
        aria-hidden="true"
      >
        {hasBody && !expanded ? (
          <Chevron open={false} className={tone.word} />
        ) : (
          status.glyph
        )}
      </span>
      <span className="min-w-0 flex-1 break-words">
        <span className={`font-semibold ${tone.word}`}>{status.word}</span>{" "}
        <span className="text-foreground">
          {humanizeBlockLabel(block.label)}
        </span>
        {status.note === null ? null : (
          <span className="text-muted-foreground">{`${SEP}${status.note}`}</span>
        )}
      </span>
      {(expanded ? elapsed : summary) ? (
        <span className="shrink-0 whitespace-nowrap text-[11.5px] tabular-nums text-muted-foreground">
          {expanded ? elapsed : summary}
        </span>
      ) : null}
    </>
  );
  const headerClass = `flex w-full min-w-0 items-start gap-2 px-3 text-left text-[12.5px] leading-5 ${
    reason === null ? "py-[11px]" : "pb-1 pt-[11px]"
  }`;

  return (
    <div
      data-testid="copilot-test-card"
      data-tone={status.tone}
      className={`overflow-hidden rounded-lg border ${tone.card}`}
    >
      <button
        type="button"
        className={headerClass}
        aria-expanded={hasBody ? expanded : undefined}
        title={`Highlight ${block.label} on canvas`}
        onClick={() => {
          onSelect?.(block.label);
          if (hasBody) onToggle();
        }}
      >
        {header}
      </button>
      {reason === null ? null : (
        <p className="px-3 pb-[11px] pl-[34px] text-[12px] leading-[1.5] text-muted-foreground">
          {reason}
        </p>
      )}
      {expanded ? (
        <div className="flex flex-col gap-2 border-t border-border bg-slate-elevation2 px-3 py-2.5">
          {diff === null ? null : (
            <FCardCodeChange
              diff={diff}
              open={diffOpen}
              onToggle={onDiffToggle}
            />
          )}
          {diff !== null &&
          (actions.length > 0 ||
            blockFailure !== null ||
            block.activity.length > 0) ? (
            <div aria-hidden="true" className="h-px bg-border" />
          ) : null}
          {actions.map((action, i) => (
            <FCardActionRow
              key={action.actionId}
              action={action}
              revealing={i === reveal.revealingIndex}
              flash={reveal.flashing(i)}
              failureText={failureOf(action, i)}
            />
          ))}
          {blockFailure === null ? null : (
            <div className="-mx-1.5 flex flex-col gap-1.5 rounded-md bg-rose-500/[0.08] px-1.5 py-2">
              <div className="flex items-start gap-2 text-[12px] leading-[1.5]">
                <span className="flex h-[18px] w-3.5 shrink-0 items-center justify-center">
                  <Cross2Icon
                    aria-hidden="true"
                    className="size-[13px] text-rose-600 dark:text-rose-500"
                  />
                </span>
                <span className="font-semibold text-rose-700 dark:text-rose-300">
                  Error
                </span>
              </div>
              <FFailureText text={blockFailure} />
            </div>
          )}
          {block.activity.map((entry) => (
            <ActivityRow key={entry.id} entry={entry} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

// A failed call's error: the sanitized detail when the stream carried it, else
// its saved result line.
function callFailureText(entry: ActivityEntry): string | null {
  if (entry.kind !== "tool_result" || entry.success !== false) return null;
  return entry.detail ?? (entry.text !== callLabel(entry) ? entry.text : null);
}

function FCardCodeChange({
  diff,
  open,
  onToggle,
}: {
  diff: CodeWriteDiff;
  open: boolean;
  onToggle: () => void;
}) {
  const counts = (
    <span className="font-mono text-[11px] tabular-nums">
      <span className="text-emerald-700 dark:text-emerald-500">{`+${diff.added}`}</span>{" "}
      <span className="text-rose-700 dark:text-rose-500">{`−${diff.removed}`}</span>
    </span>
  );
  const lineClass =
    "flex w-fit items-center gap-1.5 text-[12px] leading-[1.5] text-muted-foreground";
  // A patch dropped for size keeps its counts; there is nothing to open.
  if (diff.patch === undefined) {
    return (
      <div
        className={`${lineClass} pl-[18px]`}
        title={
          diff.patchDropped
            ? "The diff was too large to keep, so only its line counts were saved."
            : undefined
        }
      >
        <span>Code change</span>
        {counts}
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-1.5">
      <button
        type="button"
        aria-expanded={open}
        onClick={onToggle}
        className={`${lineClass} hover:text-foreground`}
      >
        <Chevron open={open} />
        <span>Code change</span>
        {counts}
      </button>
      {open ? <DiffPatch patch={diff.patch} /> : null}
    </div>
  );
}

function FFittedSteps({ steps }: { steps: string[] }) {
  const ref = useRef<HTMLSpanElement>(null);
  const [[head, tail], setFit] = useState<[string, string]>(() => [
    steps.join(STEP_SEPARATOR),
    "",
  ]);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const context = document.createElement("canvas").getContext("2d");
    if (!context) return;
    const style = getComputedStyle(el);
    context.font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
    const fit = () =>
      setFit(
        fitSteps(steps, el.clientWidth, (t) => context.measureText(t).width),
      );
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(el);
    return () => observer.disconnect();
  }, [steps]);
  // whitespace-pre keeps the tail's leading separator, which a flex item
  // would otherwise strip; the head's huge shrink factor makes it give up
  // its width before the tail does.
  return (
    <span
      ref={ref}
      aria-hidden="true"
      className="flex min-w-0 flex-1 overflow-hidden"
    >
      <span className="min-w-0 overflow-hidden text-ellipsis whitespace-pre [flex-shrink:9999]">
        {head}
      </span>
      {tail ? (
        <span className="min-w-0 overflow-hidden text-ellipsis whitespace-pre">
          {tail}
        </span>
      ) : null}
    </span>
  );
}

// One call in an expanded step. A result reads inline beside the call and
// opens to its full text; an error opens as its own box.
function FCallItem({
  entry,
  count,
  detail,
  inlineResult,
  defaultOpen,
  evidence,
  turnEnded,
}: {
  entry: ActivityEntry;
  count: number;
  detail: React.ReactNode | null;
  inlineResult: string | null;
  defaultOpen: boolean;
  evidence?: React.ReactNode;
  turnEnded: boolean;
}) {
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const expandable = detail !== null || inlineResult !== null;
  const open = expandable && (userOpen ?? defaultOpen);
  const Icon =
    CALL_KIND_ICON[
      entry.toolName === undefined ? "other" : toolCallKind(entry.toolName)
    ];
  const failed = entry.kind === "tool_result" && entry.success === false;
  // A cancelled turn can end with a call that never got its result.
  const pending = entry.kind === "tool_call" && !turnEnded;
  const diffs = entry.codeDiffs ?? [];
  const steps = failed ? undefined : entry.browserSteps;
  const lineClass = `flex w-full min-w-0 items-center gap-1.5 py-px text-left text-[12px] leading-[1.5] ${STEP_TEXT}`;
  const inner = (
    <>
      {expandable ? <Chevron open={open} /> : null}
      <Icon
        aria-hidden="true"
        className="size-3 shrink-0 text-muted-foreground"
      />
      {steps?.length ? (
        // A finished browser code call is labelled by what it did.
        <>
          <span className="sr-only">{steps.join(STEP_SEPARATOR)}</span>
          <FFittedSteps steps={steps} />
          {count > 1 ? (
            <span className="shrink-0 text-muted-foreground">{`×${count}`}</span>
          ) : null}
          <AttemptsBadge attempts={entry.attempts} />
        </>
      ) : (
        <span className="min-w-0 break-words">
          {callLabel(entry)}
          {count > 1 ? (
            <span className="text-muted-foreground">{` ×${count}`}</span>
          ) : null}
          {failed ? (
            <span className="text-muted-foreground"> · attempt failed</span>
          ) : null}
          <AttemptsBadge attempts={entry.attempts} />
        </span>
      )}
      {/* Shown beside the label until opened, then in full below it; the
          button's name stays the call, not its result. */}
      {inlineResult === null || open || steps?.length ? null : (
        <span
          aria-hidden="true"
          className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted-foreground"
        >
          {inlineResult}
        </span>
      )}
      {diffs.length === 0 ? null : (
        <DiffCounts
          added={diffs.reduce((n, d) => n + d.added, 0)}
          removed={diffs.reduce((n, d) => n + d.removed, 0)}
        />
      )}
      {pending ? (
        <>
          <Spinner small />
          <span className="sr-only">calling</span>
        </>
      ) : null}
    </>
  );
  return (
    <li>
      {!expandable ? (
        <div className={`${lineClass} pl-[18px]`}>{inner}</div>
      ) : (
        <button
          type="button"
          aria-expanded={open}
          className={`${lineClass} hover:text-foreground`}
          onClick={() => setUserOpen(!open)}
        >
          {inner}
        </button>
      )}
      {open && inlineResult !== null ? (
        <p className="mb-0.5 ml-[36px] whitespace-pre-wrap break-words font-mono text-[11px] leading-[1.5] text-muted-foreground">
          {inlineResult}
        </p>
      ) : null}
      {evidence}
      {open && detail !== null ? <FDetailBox>{detail}</FDetailBox> : null}
    </li>
  );
}

function FActivityLogRow({
  row,
  open,
  onToggle,
  diffOpen,
  diffPeek,
  onDiffToggle,
  cardOpen,
  onCardToggle,
  turnEnded,
  onBlockSelect,
  outcomeReasonFallback,
  outcomeOwnerKey,
}: {
  row: ActivityRowModel;
  open: boolean;
  onToggle: () => void;
  diffOpen: (label: string) => boolean;
  diffPeek: (label: string) => boolean;
  onDiffToggle: (label: string) => void;
  cardOpen: (blockKey: string, fallback: boolean) => boolean;
  onCardToggle: (blockKey: string, open: boolean) => void;
  turnEnded: boolean;
  onBlockSelect?: (label: string) => void;
  outcomeReasonFallback?: string | null;
  outcomeOwnerKey?: string | null;
}) {
  const last = row.entries[row.entries.length - 1];
  // Time-derived like the recorded-action reveal, so a hydrated row (no
  // arrival stamp) falls straight through to the full string on first render.
  const reasonShown =
    row.reason === null
      ? 0
      : revealedCharsAt(row.reason.length, Date.now() - (row.reasonAt ?? 0));
  const reasonRevealing =
    row.reason !== null && reasonShown < row.reason.length;
  useFrameTick(reasonRevealing);
  // Every step keeps its whole sentence where it was spoken. While it types out, the rest of
  // the string stays in flow, transparent, so the line never grows and later evidence stays put.
  const reasonNode =
    row.reason === null ? null : (
      <button
        type="button"
        aria-expanded={open}
        onClick={onToggle}
        data-testid="copilot-reason"
        className={`mb-px mt-2 break-words text-left text-[13px] leading-[1.55] text-foreground [overflow-wrap:anywhere] dark:text-slate-200 ${open || row.kind === null ? "" : "line-clamp-2"}`}
      >
        {row.reason.slice(0, reasonShown)}
        <span
          aria-hidden="true"
          className={reasonRevealing ? "animate-pulse" : "opacity-0"}
        >
          {"▌"}
        </span>
        <span className="text-transparent">
          {row.reason.slice(reasonShown)}
        </span>
      </button>
    );

  // A block its run call ran reads as one card in that call's place, and the
  // row's newest code change moves into the card of the newest run of its label:
  // its first card there, since loop iterations share one.
  const blocksByCall = blocksByRunCall(row);
  const diffCardKey = new Map<string, string>();
  const claimDiffs = (blocks: BlockState[]) => {
    const claimed = new Set<string>();
    for (const block of blocks) {
      if (
        !claimed.has(block.label) &&
        row.codeDiffs.some((diff) => diff.label === block.label)
      ) {
        claimed.add(block.label);
        diffCardKey.set(block.label, blockIdentity(block));
      }
    }
  };
  if (blocksByCall.size === 0) claimDiffs(row.blocks);
  // A retry whose patched block has not started yet keeps its new patch on the
  // row, not in the earlier attempt's card.
  for (const entry of row.entries) {
    for (const diff of entry.codeDiffs ?? []) diffCardKey.delete(diff.label);
    const hosted = blocksByCall.get(toolCallIdOf(entry) ?? "");
    if (hosted !== undefined) claimDiffs(hosted);
  }
  const isCardOpen = (block: BlockState) =>
    cardOpen(
      blockIdentity(block),
      block.state === "running" || block.state === "failed",
    );
  // A run call's error reads once: on the last card whose block failed.
  const cardNode = (
    block: BlockState,
    host: ActivityEntry | undefined,
    carriesCallFailure: boolean,
  ) => {
    const key = blockIdentity(block);
    const diff =
      diffCardKey.get(block.label) === key
        ? (row.codeDiffs.find((d) => d.label === block.label) ?? null)
        : null;
    const reasonFallback =
      key === outcomeOwnerKey || block.outcome === "not_demonstrated"
        ? outcomeReasonFallback
        : null;
    const cardIsOpen = isCardOpen(block);
    return (
      <FTestRunCard
        key={key}
        block={block}
        diff={diff}
        diffOpen={diffOpen(block.label)}
        onDiffToggle={() => onDiffToggle(block.label)}
        callFailure={
          host !== undefined && carriesCallFailure
            ? callFailureText(host)
            : null
        }
        outcomeReason={normalizeOutcomeReason(
          block.outcomeReason ?? reasonFallback,
        )}
        ownsVerdict={key === outcomeOwnerKey}
        turnEnded={turnEnded}
        open={cardIsOpen}
        onToggle={() => onCardToggle(key, cardIsOpen)}
        onSelect={onBlockSelect}
      />
    );
  };
  const diffNodes = row.codeDiffs
    .filter((diff) => !diffCardKey.has(diff.label))
    .map((diff) => (
      <FCodeWriteDiff
        key={diff.label}
        diff={diff}
        open={diffOpen(diff.label)}
        peek={diffPeek(diff.label)}
        onToggle={() => onDiffToggle(diff.label)}
      />
    ));

  const diffEvidence = (
    <div className="mb-1 ml-[18px] mt-0.5 flex flex-col gap-1">{diffNodes}</div>
  );
  const cardStack = (cards: React.ReactNode[], key?: string) => (
    <div key={key} className="my-1 flex flex-col gap-1.5">
      {cards}
    </div>
  );

  // A block observed with no step of its own keeps its card as the line.
  if (row.entries.length === 0 && row.draftingLabels === undefined) {
    return (
      <div className="flex flex-col">
        {reasonNode}
        {cardStack(
          row.blocks.map((block) => cardNode(block, undefined, false)),
        )}
        {open && diffNodes.length > 0 ? diffEvidence : null}
      </div>
    );
  }

  const failedLast = last?.kind === "tool_result" && last.success === false;
  // The sentence above says why; the line says what the calls were.
  const title = callRollup(row.entries, turnEnded) ?? "Working";
  const failedBlocks = failedRowBlocks(row);
  const passed = passedBlockCount(row);
  const rowAdded = row.codeDiffs.reduce((n, d) => n + d.added, 0);
  const rowRemoved = row.codeDiffs.reduce((n, d) => n + d.removed, 0);
  const kindWord = row.kind === null ? undefined : ACTIVITY_KIND_WORD[row.kind];
  const calls = condenseCalls(row.entries);
  const resultShown = (entry: ActivityEntry) =>
    entry.kind === "tool_result" && entry.text !== callLabel(entry);
  const hasDetail =
    calls.length > 0 || row.blocks.length > 0 || row.codeDiffs.length > 0;

  const lineText = (
    <span
      className={`min-w-0 flex-1 ${row.live ? STEP_TEXT : "text-muted-foreground"}`}
    >
      {kindWord === undefined ? null : (
        <span className="sr-only">{kindWord} · </span>
      )}
      {row.draftingLabels === undefined ? (
        title
      ) : (
        // Raw authored identifiers, humanized like every other surface. Capping
        // the head keeps already-read text still while only the tail counts up.
        <>
          <span>Writing the workflow code</span>
          {row.draftingLabels.length === 0 ? null : (
            <span className="font-normal text-muted-foreground">
              {` · ${row.draftingLabels
                .slice(0, MAX_DRAFTING_LABELS)
                .map(humanizeBlockLabel)
                .join(", ")}`}
              {row.draftingLabels.length > MAX_DRAFTING_LABELS
                ? ` +${row.draftingLabels.length - MAX_DRAFTING_LABELS} more`
                : ""}
            </span>
          )}
        </>
      )}
      {failedLast ? (
        <span className="font-normal text-muted-foreground">
          {SEP}attempt failed
        </span>
      ) : null}
      <AttemptsBadge attempts={last?.attempts} />
      {row.codeDiffs.length === 0 ? null : (
        <>
          <span className="font-normal text-muted-foreground">{SEP}</span>
          <DiffCounts added={rowAdded} removed={rowRemoved} />
        </>
      )}
      {passed === null ? null : (
        <span className="font-normal text-muted-foreground">{`${SEP}${passed} of ${passed} passed`}</span>
      )}
      {failedBlocks.length === 0 ? null : (
        <span className="font-normal">
          <span className="text-muted-foreground">{SEP}</span>
          {failedBlocks.map((block, i) => (
            <span key={block.workflowRunBlockId || block.label}>
              {i === 0 ? null : " "}
              <FFailurePin block={block} />
            </span>
          ))}
        </span>
      )}
    </span>
  );
  // An open row's running card already carries the loader.
  const cardShowsLive =
    open &&
    row.blocks.some((block) => block.state === "running" && isCardOpen(block));
  const line = (
    <>
      <span className="flex h-[19px] w-3 shrink-0 items-center">
        {hasDetail ? <Chevron open={open} /> : null}
      </span>
      {row.live && !cardShowsLive ? (
        <span className="flex h-[19px] shrink-0 items-center">
          <Spinner small />
          <span className="sr-only">in progress</span>
        </span>
      ) : null}
      {lineText}
    </>
  );
  const lineClass =
    "flex w-full min-w-0 items-start gap-1.5 py-px text-left text-[12.5px] leading-[1.5]";

  // Collapsing a row must not hide that its run's outcome went unconfirmed.
  const outcomeNotes = open
    ? []
    : blockOutcomeNotes(
        row.blocks,
        outcomeOwnerKey,
        outcomeReasonFallback,
        row.blocks.length > 1,
      );

  const hostedBlocks = (entry: ActivityEntry) =>
    blocksByCall.get(toolCallIdOf(entry) ?? "") ?? [];
  let diffHost = -1;
  if (diffNodes.length > 0) {
    calls.forEach(({ entry }, i) => {
      if (
        (entry.codeDiffs?.length ?? 0) > 0 &&
        hostedBlocks(entry).length === 0
      ) {
        diffHost = i;
      }
    });
  }

  // Calls read as one bordered list; a card breaks the list where its call
  // was, so the log keeps the order things happened in.
  const listClass =
    "mb-1 ml-[18px] mt-0.5 flex list-none flex-col gap-px border-l border-border pl-2";
  const segments: React.ReactNode[] = [];
  let items: React.ReactNode[] = [];
  const flushItems = () => {
    if (items.length === 0) return;
    segments.push(
      <ul key={`calls-${segments.length}`} className={listClass}>
        {items}
      </ul>,
    );
    items = [];
  };
  if (diffNodes.length > 0 && diffHost === -1) {
    items.push(<li key="diffs">{diffEvidence}</li>);
  }
  const carded = new Set<BlockState>();
  calls.forEach(({ entry, count }, i) => {
    const hosted = hostedBlocks(entry);
    const failed = entry.kind === "tool_result" && entry.success === false;
    const result =
      failed && resultShown(entry) ? (
        <pre className="whitespace-pre-wrap break-words font-mono text-[11.5px] leading-[1.5] text-rose-700 dark:text-rose-300">
          {entry.text}
        </pre>
      ) : null;
    const callItem = (
      <FCallItem
        key={toolCallIdOf(entry) ?? entry.id}
        entry={entry}
        turnEnded={turnEnded}
        count={count}
        detail={result}
        inlineResult={!failed && resultShown(entry) ? entry.text : null}
        // The exact server error is the evidence a failed step owes.
        defaultOpen={failed}
        evidence={i === diffHost ? diffEvidence : null}
      />
    );
    if (hosted.length > 0) {
      // A failed card carries its call's error; with none to carry it, the
      // call keeps its own row above the cards.
      if (result !== null && !hosted.some((b) => b.state === "failed")) {
        items.push(callItem);
      }
      flushItems();
      hosted.forEach((block) => carded.add(block));
      const lastFailedHosted = hosted.map((b) => b.state).lastIndexOf("failed");
      segments.push(
        cardStack(
          hosted.map((block, blockIndex) =>
            cardNode(block, entry, blockIndex === lastFailedHosted),
          ),
          `cards-${toolCallIdOf(entry) ?? entry.id}`,
        ),
      );
      return;
    }
    items.push(callItem);
  });
  flushItems();
  // A block whose call was condensed into an identical sibling still shows.
  const uncarded = row.blocks.filter((block) => !carded.has(block));
  if (uncarded.length > 0) {
    segments.push(
      cardStack(
        uncarded.map((block) => cardNode(block, undefined, false)),
        "cards-rest",
      ),
    );
  }
  const body = <>{segments}</>;

  return (
    <div
      className="flex flex-col"
      data-tool-call-id={
        row.entries.length === 1 ? toolCallIdOf(row.entries[0]!) : undefined
      }
    >
      {reasonNode}
      {hasDetail ? (
        <button
          type="button"
          data-activity-line="true"
          aria-expanded={open}
          onClick={onToggle}
          className={`${lineClass} cursor-pointer`}
        >
          {line}
        </button>
      ) : (
        <div data-activity-line="true" className={lineClass}>
          {line}
        </div>
      )}
      {outcomeNotes.map(({ key, note }) => (
        <FOutcomeNote key={key} note={note} />
      ))}
      {open && hasDetail ? body : null}
    </div>
  );
}

function blockOutcomeNotes(
  blocks: BlockState[],
  outcomeOwnerKey: string | null | undefined,
  outcomeReasonFallback: string | null | undefined,
  named: boolean,
): { key: string; note: string }[] {
  return blocks.flatMap((b) => {
    const note = collapsedOutcomeNote(
      b,
      blockIdentity(b) === outcomeOwnerKey,
      outcomeReasonFallback,
    );
    if (note === null) return [];
    return [
      {
        key: blockIdentity(b),
        note: named ? `${humanizeBlockLabel(b.label)}: ${note}` : note,
      },
    ];
  });
}

function FOutcomeNote({ note }: { note: string }) {
  return (
    <div className="ml-[18px] mt-0.5 text-[12px] leading-[1.5] text-amber-700 dark:text-amber-200/80">
      {note}
    </div>
  );
}

function FTurnFoldHeader({
  summary,
  open,
  onToggle,
}: {
  summary: FinishedTurnSummary;
  open: boolean;
  onToggle: () => void;
}) {
  const failures =
    summary.failedTests === 0
      ? ""
      : ` · ${summary.failedTests} failed ${summary.failedTests === 1 ? "test" : "tests"}${summary.fixed ? ", fixed" : ""}`;
  return (
    <button
      type="button"
      data-activity-fold="true"
      aria-expanded={open}
      onClick={onToggle}
      className="flex w-full min-w-0 items-start gap-1.5 pb-1.5 pt-0.5 text-left text-[12.5px] leading-[1.5] text-muted-foreground hover:text-foreground"
    >
      <span className="flex h-[19px] shrink-0 items-center">
        <Chevron open={open} />
      </span>
      <span className="min-w-0">
        {`Worked through ${summary.steps} steps${failures}`}
        {summary.stillFailing.map((block) => (
          <span key={block.workflowRunBlockId || block.label}>
            {SEP}
            <FFailurePin block={block} />
          </span>
        ))}
      </span>
      {summary.stillFailing.length > 0 ? null : (
        <span
          aria-hidden="true"
          className="ml-1 mt-[9px] h-px min-w-4 flex-1 bg-border"
        />
      )}
    </button>
  );
}

function FActivityLog({
  turn,
  log,
  turnEnded,
  onBlockSelect,
  interactionRef,
  anchoredAfterRow,
  anchoredBeforeRows = NO_ANCHORED_ITEMS,
}: FActivityLogProps) {
  const outcomeReasonFallback = notConfirmedDisplayReason(turn);
  const outcomeOwnerKey = outcomeNotConfirmedOwnerKey(turn);
  const { rows, focusIndex } = log;
  // Signed, not a bare id set: a click on the live row has to be able to mean
  // "closed", or folding the active row would silently pin it open instead.
  const [override, setOverride] = useState<ReadonlyMap<string, boolean>>(
    () => new Map(),
  );
  const [foldOpen, setFoldOpen] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);
  const ownInteractionRef = useRef<string | null>(null);
  const lastInteractedRow = interactionRef ?? ownInteractionRef;
  useEffect(() => {
    const rowId = lastInteractedRow.current;
    setOverride(new Map());
    if (!turnEnded || rowId === null) return;
    const timer = window.setTimeout(() => {
      const row = Array.from(
        logRef.current?.querySelectorAll<HTMLElement>(
          "[data-activity-row-id]",
        ) ?? [],
      ).find((candidate) => candidate.dataset.activityRowId === rowId);
      // A finished turn folds its rows, so focus falls back to the fold.
      (
        row?.querySelector<HTMLButtonElement>("button") ??
        logRef.current?.querySelector<HTMLButtonElement>("[data-activity-fold]")
      )?.focus();
      lastInteractedRow.current = null;
    }, 0);
    return () => window.clearTimeout(timer);
  }, [lastInteractedRow, turn.turnId, turnEnded]);
  const toggle = useCallback(
    (id: string, open: boolean) => {
      lastInteractedRow.current = id;
      setOverride((prev) => new Map(prev).set(id, !open));
    },
    [lastInteractedRow],
  );
  // A diff or a test card inside a row, keyed `<kind>:<row id>:<id>`.
  const toggleEvidence = useCallback(
    (rowId: string, key: string, open: boolean) => {
      lastInteractedRow.current = rowId;
      setOverride((prev) => {
        const next = new Map(prev);
        next.set(key, !open);
        // Expanding evidence is also an explicit request to keep its parent
        // visible when the automatic frontier advances.
        if (!open) next.set(rowId, true);
        return next;
      });
    },
    [lastInteractedRow],
  );

  if (!turnEnded && rows.length === 0) {
    return <InstantAckPlaceholder />;
  }

  // One row already reads as one line, so only a longer finished turn folds.
  const finished = turnEnded
    ? summarizeFinishedTurn(rows, turn.turnFacts)
    : null;
  const summary = finished !== null && finished.steps > 1 ? finished : null;

  // Folded, a card from before or during the first step still reads first.
  const folded = summary !== null && !foldOpen;
  const leadingCards = [
    ...anchoredBeforeRows,
    ...(rows[0] ? (anchoredAfterRow?.get(rows[0].id) ?? []) : []),
  ];
  return (
    <div ref={logRef} className="flex flex-col gap-1.5">
      {folded ? logAnchoredNodes(leadingCards.filter(isCard)) : null}
      {summary === null ? null : (
        <FTurnFoldHeader
          summary={summary}
          open={foldOpen}
          onToggle={() => setFoldOpen((v) => !v)}
        />
      )}
      {/* A block that owns the turn's unconfirmed verdict suppresses the turn
          card, so the fold has to keep that note visible itself. */}
      {summary === null || foldOpen
        ? null
        : blockOutcomeNotes(
            rows
              .flatMap((row) => row.blocks)
              .filter((b) => blockIdentity(b) === outcomeOwnerKey)
              .slice(-1),
            outcomeOwnerKey,
            outcomeReasonFallback,
            true,
          ).map(({ key, note }) => <FOutcomeNote key={key} note={note} />)}
      {/* Folding the steps must not hide the cards that happened among them. */}
      {folded
        ? logAnchoredNodes(
            rows
              .slice(1)
              .flatMap((row) => anchoredAfterRow?.get(row.id) ?? [])
              .filter(isCard),
          )
        : logAnchoredNodes(anchoredBeforeRows)}
      {summary !== null && !foldOpen
        ? null
        : rows.map((row, i) => {
            const focused = i === focusIndex;
            const rowOverride = override.get(row.id);
            const autoFocused = rowOverride === undefined && focused;
            const open = rowOverride ?? focused;
            const diffOpen = (label: string) =>
              override.get(`diff:${row.id}:${label}`) ?? false;
            const diffPeek = (label: string) =>
              autoFocused &&
              !diffOpen(label) &&
              row.codeDiffs.some(
                (candidate) =>
                  candidate.label === label && candidate.patch !== undefined,
              );
            return [
              <div
                key={row.id}
                data-activity-row-id={row.id}
                onFocusCapture={() => {
                  lastInteractedRow.current = row.id;
                }}
              >
                <FActivityLogRow
                  row={row}
                  open={open}
                  onToggle={() => toggle(row.id, open)}
                  diffOpen={diffOpen}
                  diffPeek={diffPeek}
                  onDiffToggle={(label) =>
                    toggleEvidence(
                      row.id,
                      `diff:${row.id}:${label}`,
                      diffOpen(label),
                    )
                  }
                  cardOpen={(blockKey, fallback) =>
                    override.get(`card:${row.id}:${blockKey}`) ?? fallback
                  }
                  onCardToggle={(blockKey, cardIsOpen) =>
                    toggleEvidence(
                      row.id,
                      `card:${row.id}:${blockKey}`,
                      cardIsOpen,
                    )
                  }
                  turnEnded={turnEnded}
                  onBlockSelect={onBlockSelect}
                  outcomeReasonFallback={outcomeReasonFallback}
                  outcomeOwnerKey={outcomeOwnerKey}
                />
              </div>,
              ...logAnchoredNodes(anchoredAfterRow?.get(row.id) ?? []),
            ];
          })}
    </div>
  );
}

// The typed budget-expiry record, as its own line so a folded turn still says
// why it stopped. When no report was produced the terminal prose says so.
function FBudgetLimitNote({ budget }: { budget: BudgetExpiryState }) {
  const limit = budget.source === "deadline" ? "time" : "model-call";
  return (
    <div
      data-testid="copilot-budget-limit"
      className={`flex items-start gap-[7px] text-[12px] leading-[1.5] ${STEP_TEXT}`}
    >
      <StopIcon
        aria-hidden="true"
        className="mt-[3px] size-3 shrink-0 text-muted-foreground"
      />
      <span>
        {`Reached this turn's ${limit} limit. Copilot stopped starting new work${
          budget.reportProduced === true ? " and reported what it found" : ""
        }.`}
      </span>
    </div>
  );
}

// Something that happened during a turn and renders where it happened: after the activity row
// holding its tool call. When that row is not on screen (a long turn keeps only its newest
// activity), `at` places it among the rows by time; without it, it sits above the reply.
export interface AnchoredTurnItem {
  key: string;
  toolCallId: string | null;
  at?: string | null;
  // A small item: neighbors placed together share one wrapping row, and a folded turn hides it.
  inline?: boolean;
  node: React.ReactNode;
}

function placeAnchoredItems(
  rows: ActivityRowModel[],
  anchored: AnchoredTurnItem[],
): {
  anchoredAfterRow: Map<string, AnchoredTurnItem[]>;
  anchoredBeforeRows: AnchoredTurnItem[];
  unanchored: AnchoredTurnItem[];
} {
  const anchoredAfterRow = new Map<string, AnchoredTurnItem[]>();
  const anchoredBeforeRows: AnchoredTurnItem[] = [];
  const unanchored: AnchoredTurnItem[] = [];
  for (const item of anchored) {
    let row =
      item.toolCallId === null
        ? undefined
        : rows[rowIndexOfToolCall(rows, item.toolCallId)];
    const atMs = row === undefined ? parseUtcIsoMs(item.at) : null;
    if (atMs !== null && rows.length > 0) {
      // Rows can sit out of start order when parallel calls settle out of order, so pick the
      // latest start, not the last row shown.
      let latestMs = -Infinity;
      for (const candidate of rows) {
        const startedMs = parseUtcIsoMs(candidate.startedAt);
        if (startedMs !== null && startedMs <= atMs && startedMs >= latestMs) {
          latestMs = startedMs;
          row = candidate;
        }
      }
      if (row === undefined) {
        anchoredBeforeRows.push(item);
        continue;
      }
    }
    if (row === undefined) {
      unanchored.push(item);
    } else {
      anchoredAfterRow.set(row.id, [
        ...(anchoredAfterRow.get(row.id) ?? []),
        item,
      ]);
    }
  }
  return { anchoredAfterRow, anchoredBeforeRows, unanchored };
}

const isCard = (item: AnchoredTurnItem) => !item.inline;

function anchoredNodes(
  items: AnchoredTurnItem[],
  cardClass: string | undefined,
  inlineClass: string,
) {
  const groups: AnchoredTurnItem[][] = [];
  for (const item of items) {
    const last = groups[groups.length - 1];
    if (item.inline && last?.[0]?.inline) last.push(item);
    else groups.push([item]);
  }
  return groups.map((group) => {
    const first = group[0]!;
    return first.inline ? (
      <div key={first.key} className={`flex flex-wrap gap-1.5 ${inlineClass}`}>
        {group.map((item) => (
          <Fragment key={item.key}>{item.node}</Fragment>
        ))}
      </div>
    ) : (
      <div key={first.key} className={cardClass}>
        {first.node}
      </div>
    );
  });
}

const logAnchoredNodes = (items: AnchoredTurnItem[]) =>
  anchoredNodes(items, `${TURN_ROW_OUTSET} py-0.5`, "ml-[18px] py-0.5");

function AnchoredFallback({ items }: { items: AnchoredTurnItem[] }) {
  return <>{anchoredNodes(items, undefined, TURN_ROW_INSET)}</>;
}

interface DetailViewProps {
  turn: TurnNarrativeState;
  onBlockSelect?: (label: string) => void;
  workingRowActive?: boolean;
  activityInteractionRef?: { current: string | null };
  anchored: AnchoredTurnItem[];
}

function DetailView({
  turn,
  onBlockSelect,
  workingRowActive,
  activityInteractionRef,
  anchored,
}: DetailViewProps) {
  const collapsedOutcomeReason = notConfirmedDisplayReason(turn);
  const outcomeOwnerKey = outcomeNotConfirmedOwnerKey(turn);
  const observedBlocks = turn.blocks.filter(hasObservedBlockEvidence);
  const hasBlocks = observedBlocks.length > 0;
  const designStarted = turn.designStarted;
  const designOpen = designStarted && !turn.designEnded;
  // Hide the "Designed the workflow" cluster on terminal turns that produced
  // no draft (Q&A / clarify / refuse routes occasionally emit design_start
  // before the agent decides not to build). Live turns still surface it so a
  // long design phase isn't silently invisible.
  const hasDraft = (turn.draft?.blockCount ?? 0) > 0;
  const showDesign = designStarted && (hasDraft || hasBlocks || !turn.terminal);
  const showChecklist = showPhaseChecklist(turn);
  const preBlockNarration = turn.designActivity.filter(
    (e) => e.kind === "narration",
  );
  const log = useMemo(
    () => (showChecklist ? deriveActivityLog(turn) : null),
    [showChecklist, turn],
  );
  const { anchoredAfterRow, anchoredBeforeRows, unanchored } = useMemo(
    () =>
      placeAnchoredItems(
        // The log shows a placeholder instead of rows until a live turn has one.
        log && (turn.terminal !== null || log.rows.length > 0) ? log.rows : [],
        anchored,
      ),
    [anchored, log, turn.terminal],
  );

  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex flex-col gap-2.5">
        {log ? (
          <div className={TURN_ROW_INSET}>
            <FActivityLog
              key={turn.turnId ?? ""}
              turn={turn}
              log={log}
              turnEnded={turn.terminal !== null}
              onBlockSelect={onBlockSelect}
              interactionRef={activityInteractionRef}
              anchoredAfterRow={anchoredAfterRow}
              anchoredBeforeRows={anchoredBeforeRows}
            />
          </div>
        ) : showDesign ? (
          <div className={TURN_ROW_INSET}>
            <FDesignRow
              done={!designOpen}
              blockLabels={turn.draft?.blockLabels ?? []}
              activity={turn.designActivity}
            />
          </div>
        ) : preBlockNarration.length > 0 ? (
          <div className={TURN_ROW_INSET}>
            {preBlockNarration.map((e) => (
              <FProse key={e.id} text={e.text} muted italic />
            ))}
          </div>
        ) : null}

        {!showChecklist && hasBlocks ? (
          <div className={`flex flex-col gap-1 ${TURN_ROW_INSET}`}>
            {observedBlocks.map((b) => (
              <FBlockRun
                key={b.workflowRunBlockId || b.label}
                block={b}
                turnEnded={turn.terminal !== null}
                onSelect={onBlockSelect}
                outcomeReasonFallback={collapsedOutcomeReason}
                ownsOutcomeNotConfirmed={blockIdentity(b) === outcomeOwnerKey}
              />
            ))}
          </div>
        ) : null}

        {!hasBlocks && !designStarted && !turn.terminal && !workingRowActive ? (
          <div className={TURN_ROW_INSET}>
            <div className="pl-9 text-[12px] italic text-muted-foreground dark:text-slate-500">
              Working…
            </div>
          </div>
        ) : null}

        {notConfirmedOutcome(turn) !== null && outcomeOwnerKey === null ? (
          <div className="flex items-start gap-2 rounded-md border border-amber-400/30 bg-amber-500/10 px-2.5 py-1.5">
            <span
              aria-hidden="true"
              className="text-[11px] font-bold text-amber-700 dark:text-amber-300"
            >
              !
            </span>
            <div className="text-[12px] leading-[1.5] text-amber-700 dark:text-amber-200/90">
              <span className="font-semibold">Outcome not confirmed</span>
              {` — ${
                collapsedOutcomeReason !== null
                  ? truncateOutcomeReason(collapsedOutcomeReason)
                  : OUTCOME_NOT_CONFIRMED_REASON
              }`}
            </div>
          </div>
        ) : null}

        <AnchoredFallback items={unanchored} />

        {turn.terminal !== null &&
        turn.budgetExpiry !== null &&
        turn.budgetExpiry.reportProduced !== false ? (
          <FBudgetLimitNote budget={turn.budgetExpiry} />
        ) : null}

        {/* terminalProseTone's question branch without its evidence gate: an
            ask that followed a run keeps the rail here, beside the evidence,
            rather than replacing the card with prose-only chrome. */}
        {turn.terminal &&
        (turn.narrativeSummary ||
          turn.terminalMessage ||
          turn.budgetExpiry?.reportProduced === false) ? (
          <div
            data-testid="copilot-detail-prose"
            className={[
              TURN_ROW_INSET,
              "text-[13px] leading-[1.55]",
              isQuestionTurn(turn)
                ? QUESTION_PROSE_CLASSES
                : "text-foreground dark:text-slate-200",
            ].join(" ")}
          >
            <CopilotMarkdown
              text={humanizeJudgeText(terminalNarrativeText(turn))}
            />
          </div>
        ) : null}
      </div>
    </div>
  );
}

interface NarrativeViewProps {
  turn: TurnNarrativeState;
  onBlockSelect?: (blockLabel: string) => void;
  workingRowActive?: boolean;
  anchored?: AnchoredTurnItem[];
}

const NO_ANCHORED_ITEMS: AnchoredTurnItem[] = [];

type TerminalProseTone = "answer" | "question";

function hasRecordedTerminalEvidence(turn: TurnNarrativeState): boolean {
  return (
    turn.blocks.length > 0 ||
    (turn.draft?.blockCount ?? 0) > 0 ||
    turn.lastRunOutcome !== null
  );
}

function terminalProseTone(turn: TurnNarrativeState): TerminalProseTone | null {
  // This is intentionally driven only by the structured terminal outcome.
  // Agent language is freeform, so parsing it to decide whether the user needs
  // to respond would turn presentation into a brittle copy contract.
  if (turn.terminal !== "response" || turn.cancelled) return null;
  // A run whose outcome was not demonstrated needs its recorded outcome
  // evidence. Freeform clarification prose cannot replace that inspection
  // path.
  if (notConfirmedOutcome(turn) !== null) return null;
  if (
    turn.proposalDisposition === "review_untested" ||
    turn.proposalDisposition === "review_tested"
  ) {
    return null;
  }
  // A terminal question can follow a partial build or test. Keep recorded work
  // on the expandable evidence path rather than losing it to prose-only chrome.
  if (hasRecordedTerminalEvidence(turn)) return null;
  if (isQuestionTurn(turn)) {
    return "question";
  }
  if (
    turn.responseKind === "answer" ||
    turn.responseKind === "diagnose" ||
    turn.responseKind === "refuse" ||
    turn.responseKind === "recover"
  ) {
    return "answer";
  }
  return null;
}

function TerminalProse({
  text,
  tone,
  arrivedAt,
}: {
  text: string;
  tone: TerminalProseTone;
  arrivedAt: string | null;
}) {
  const visibleLengthRef = useRef({ text, length: text.length });
  if (visibleLengthRef.current.text !== text) {
    visibleLengthRef.current = { text, length: text.length };
  }
  const onCharacterCount = useCallback(
    (count: number) => {
      visibleLengthRef.current = { text, length: Math.max(1, count) };
    },
    [text],
  );
  const reducedMotion =
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  // A hydrated history row should never replay. The terminal timestamp is the
  // recorded arrival time for a live response, while an absent timestamp means
  // the browser cannot truthfully reconstruct the original reveal.
  const arrivalMs = parseUtcIsoMs(arrivedAt);
  const elapsedMs = arrivalMs === null ? null : Date.now() - arrivalMs;
  const visibleLength = visibleLengthRef.current.length;
  const shown =
    reducedMotion || elapsedMs === null
      ? visibleLength
      : Math.min(
          visibleLength,
          Math.max(1, revealedCharsAt(visibleLength, elapsedMs)),
        );
  const revealing = shown < visibleLength;
  const settleProgress =
    !revealing && elapsedMs !== null
      ? Math.min(
          1,
          Math.max(
            0,
            (elapsedMs - visibleLength * REVEAL_MS_PER_CHAR) /
              TERMINAL_PROSE_GRADIENT_SETTLE_MS,
          ),
        )
      : 0;
  const settling =
    !reducedMotion && elapsedMs !== null && !revealing && settleProgress < 1;
  const gradientChars = revealing
    ? TERMINAL_PROSE_GRADIENT_CHARS
    : Math.ceil(TERMINAL_PROSE_GRADIENT_CHARS * (1 - settleProgress));
  const gradientStart =
    revealing || settling ? Math.max(0, shown - gradientChars) : shown;
  useFrameTick(revealing || settling);

  return (
    <div
      data-testid="copilot-terminal-prose"
      className={[
        TURN_ROW_INSET,
        "text-[13px] leading-[1.55]",
        tone === "question"
          ? QUESTION_PROSE_CLASSES
          : "text-foreground dark:text-slate-200",
      ].join(" ")}
    >
      <div className="sr-only">
        <CopilotMarkdown text={text} />
      </div>
      <div data-testid="copilot-terminal-prose-visual" aria-hidden="true">
        <CopilotMarkdown
          text={text}
          reveal={
            revealing || settling
              ? { shown, gradientStart, onCharacterCount }
              : undefined
          }
        />
      </div>
    </div>
  );
}

export function NarrativeView({
  turn,
  onBlockSelect,
  workingRowActive,
  anchored = NO_ANCHORED_ITEMS,
}: NarrativeViewProps) {
  const proseTone = terminalProseTone(turn);
  const proseText = humanizeJudgeText(terminalNarrativeText(turn));
  const activityInteractionRef = useRef<string | null>(null);
  const answerLog = useMemo(
    () =>
      proseTone !== null && proseText && turn.designActivity.length > 0
        ? deriveActivityLog(turn)
        : null,
    [proseTone, proseText, turn],
  );
  const answerAnchored = useMemo(
    () => placeAnchoredItems(answerLog?.rows ?? [], anchored),
    [answerLog, anchored],
  );

  if (proseTone !== null && proseText) {
    const prose = (
      <TerminalProse
        text={proseText}
        tone={proseTone}
        arrivedAt={turn.endedAt}
      />
    );
    if (answerLog === null && anchored.length === 0) {
      return prose;
    }
    // An answer that took work still says what that work was, folded above it.
    return (
      <div className="flex flex-col gap-2.5">
        {answerLog === null ? null : (
          <div className={TURN_ROW_INSET}>
            <FActivityLog
              turn={turn}
              log={answerLog}
              turnEnded
              onBlockSelect={onBlockSelect}
              interactionRef={activityInteractionRef}
              anchoredAfterRow={answerAnchored.anchoredAfterRow}
              anchoredBeforeRows={answerAnchored.anchoredBeforeRows}
            />
          </div>
        )}
        <AnchoredFallback items={answerAnchored.unanchored} />
        {prose}
      </div>
    );
  }

  return (
    <DetailView
      turn={turn}
      onBlockSelect={onBlockSelect}
      workingRowActive={workingRowActive}
      activityInteractionRef={activityInteractionRef}
      anchored={anchored}
    />
  );
}
