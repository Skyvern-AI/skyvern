import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  REVEAL_MS_PER_CHAR,
  buildRevealOffsets,
  revealedCharsAt,
  revealedCountAt,
} from "./actionReveal";
import { humanizeBlockLabel } from "./blockLabel";
import { CopilotMarkdown } from "./CopilotMarkdown";
import {
  ACTIVITY_KIND_GLYPH,
  ACTIVITY_KIND_WORD,
  ActivityRow as ActivityRowModel,
  deriveActivityLog,
} from "./copilotActivityLog";
import { showPhaseChecklist } from "./copilotPhases";
import { CodeWriteDiff } from "./workflowCopilotTypes";
import {
  ActivityEntry,
  BlockState,
  RecordedActionSummary,
  TurnNarrativeState,
  awaitsUserInput,
  formatElapsed,
  humanizeJudgeText,
  isBlockOk,
  isInterimOutcome,
  notConfirmedOutcome,
  parseUtcIsoMs,
  terminalNarrativeText,
  toolActivityDisplayLabel,
} from "./narrativeState";
import { useShimmerText } from "../workflowRun/useShimmerText";
import { useThemeAsDarkOrLight } from "../../../components/useThemeAsDarkOrLight";

// Row flashes green/red for 600ms once revealed — must match the tailwind
// copilot-row-flash-* animation duration.
const FLASH_WINDOW_MS = 600;
const OUTCOME_REASON_PREVIEW_LIMIT = 140;
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

function notConfirmedDisplayReason(turn: TurnNarrativeState): string | null {
  return normalizeOutcomeReason(notConfirmedOutcome(turn)?.displayReason);
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

// The activity log's row, one grid for every kind of work: a single gutter
// glyph, the sentence, and the elapsed column. Status rides inline at the end
// of the sentence rather than as a second glyph column, so a block row and a
// step row sit on the same rails.
function FLogLine({
  glyph,
  kindWord,
  children,
  trailing,
  onClick,
  expanded,
  title,
}: {
  glyph: React.ReactNode;
  kindWord?: string;
  children: React.ReactNode;
  trailing?: React.ReactNode;
  onClick?: () => void;
  expanded?: boolean;
  title?: string;
}) {
  const body = (
    <>
      <span
        // Baseline-aligned, per the design canvas — but these glyphs do not fill
        // their box the way letters fill theirs, so sharing a baseline leaves
        // their ink centre ~1.5px below the sentence's and they read as sloppy.
        // Centring the box does not move ink; only a transform does. The offset
        // is measured, not eyeballed: render the row and compare the ink bands
        // of the two columns, and re-derive it if the type changes.
        className="inline-block -translate-y-[1.5px] text-center font-mono text-[11px] text-muted-foreground dark:text-slate-500"
        aria-hidden="true"
      >
        {glyph}
      </span>
      <span className="min-w-0 text-left text-[12.5px] leading-[1.5] text-muted-foreground dark:text-slate-400">
        {kindWord === undefined ? null : (
          <span className="sr-only">{kindWord} · </span>
        )}
        {children}
      </span>
      <span className="whitespace-nowrap font-mono text-[10.5px] tabular-nums text-muted-foreground dark:text-slate-500">
        {trailing}
      </span>
    </>
  );
  const shape =
    "grid w-full grid-cols-[18px_1fr_auto] items-baseline gap-x-2.5 py-[3px]";
  return onClick === undefined ? (
    <div className={shape}>{body}</div>
  ) : (
    <button
      type="button"
      className={`${shape} cursor-pointer text-left`}
      aria-expanded={expanded}
      onClick={onClick}
      title={title}
    >
      {body}
    </button>
  );
}

// The sentence an entry contributes, without the glyph column. The log's row
// renders it inline so status can ride at the end; a nested sub-row wraps the
// same content in its own glyph gutter.
function entryLine(
  entry: ActivityEntry,
  title?: string | null,
): { content: React.ReactNode } {
  if (entry.kind === "narration") {
    return { content: <span className="italic">{entry.text}</span> };
  }
  if (entry.kind === "tool_call") {
    return {
      content: (
        <>
          <span>
            {title ??
              entry.displayLabel ??
              toolActivityDisplayLabel(entry.toolName)}
          </span>
          <span className="text-muted-foreground dark:text-slate-500">
            {" "}
            · calling…
          </span>
          <AttemptsBadge attempts={entry.attempts} />
        </>
      ),
    };
  }
  const ok = entry.success !== false;
  const label =
    title ?? entry.displayLabel ?? toolActivityDisplayLabel(entry.toolName);
  return {
    content: (
      <>
        <span>{ok ? (title ?? entry.text) : label}</span>
        {!ok ? (
          <span className="text-muted-foreground dark:text-slate-500">
            {" "}
            · attempt failed
          </span>
        ) : null}
        <AttemptsBadge attempts={entry.attempts} />
      </>
    ),
  };
}

function ActivityRow({
  entry,
  title,
}: {
  entry: ActivityEntry;
  // Narrator-authored title for the row this entry heads. Falls through to the
  // tool-derived label when the narrator never spoke for the step.
  title?: string | null;
}) {
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
      title ?? entry.displayLabel ?? toolActivityDisplayLabel(entry.toolName);
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
        {ok ? (title ?? entry.text) : entry.text}
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
      {action.summary ? (
        <span className="text-muted-foreground dark:text-slate-500">
          {" "}
          · {action.summary}
        </span>
      ) : null}
    </FSubRow>
  );
}

interface FBlockRunProps {
  block: BlockState;
  turnEnded: boolean;
  onSelect?: (label: string) => void;
  outcomeReasonFallback?: string | null;
  ownsOutcomeNotConfirmed?: boolean;
  // Narrator title for the row this card heads. The card's own status text
  // still names the block, so the title replaces only the label here.
  rowTitle?: string | null;
  // Inside the activity log the card sheds its puck for the shared row grid,
  // so a block does not read as a different species from the steps around it.
  flat?: boolean;
  // Supplied when the log's row already names the kind of work. The block's
  // own state then rides inline as a mark instead of taking the gutter.
  rowGlyph?: React.ReactNode;
  rowKindWord?: string;
  rowTrailing?: React.ReactNode;
  rowDiffCounts?: { added: number; removed: number };
  // When the activity log owns this card's row, open/closed comes from there
  // so the row and the card never disagree about a single click.
  expansion?: { open: boolean; onToggle: () => void };
  // Historical failed attempts stay quiet in the collapsed timeline. Their
  // full failure treatment remains available in the expanded evidence.
  quietFailure?: boolean;
  // Activity-log context that should lead the block's own run evidence.
  detailBeforeBlock?: React.ReactNode;
  detailAfterBlock?: React.ReactNode;
}

function FBlockRun({
  block,
  turnEnded,
  onSelect,
  outcomeReasonFallback,
  ownsOutcomeNotConfirmed,
  rowTitle,
  flat,
  rowGlyph,
  rowKindWord,
  rowTrailing,
  rowDiffCounts,
  expansion,
  quietFailure,
  detailBeforeBlock,
  detailAfterBlock,
}: FBlockRunProps) {
  const displayLabel = rowTitle ?? humanizeBlockLabel(block.label);
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
  const prominentFailure = isFail && !quietFailure;
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
        : prominentFailure
          ? "border-rose-400/60"
          : "border-slate-500/60";
  const accentText = isRunning
    ? "text-blue-700 dark:text-blue-300"
    : isOk
      ? "text-emerald-700 dark:text-emerald-300"
      : isOutcomeNotShown
        ? "text-amber-700 dark:text-amber-300"
        : prominentFailure
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
        : prominentFailure
          ? "bg-rose-500/15"
          : "bg-slate-elevation3";

  const recordedActions = block.recordedActions;
  const hasActions =
    recordedActions !== undefined && recordedActions.length > 0;
  const durations = useMemo(
    () => (recordedActions ?? []).map((a) => a.durationMs),
    [recordedActions],
  );
  const offsets = useMemo(() => buildRevealOffsets(durations), [durations]);
  const totalMs = offsets.length > 0 ? offsets[offsets.length - 1]! : 0;
  // Time-derived, not timer-chained: recomputed from wall-clock time on
  // every render/tick so collapse, remount, and StrictMode double-invoke
  // can never restart or duplicate the reveal.
  const elapsedReveal = hasActions
    ? Date.now() - (block.recordedActionsAt ?? 0)
    : 0;
  const revealedCount = hasActions
    ? revealedCountAt(offsets, elapsedReveal)
    : 0;
  const replayingAction =
    hasActions && elapsedReveal >= 0 && revealedCount < recordedActions!.length;
  const visibleActionCount = !hasActions
    ? 0
    : elapsedReveal < 0
      ? 0
      : Math.min(
          revealedCount + (replayingAction ? 1 : 0),
          recordedActions!.length,
        );

  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const defaultOpen = isRunning || isFail || (hasActions && !turnEnded);
  const open = expansion
    ? expansion.open
    : userOpen === null
      ? defaultOpen
      : userOpen;
  // A stop stays inspectable but not self-opening: the user knows why it
  // stopped, so it should not demand attention the way a failure does. Under
  // the activity log `expansion` supplies the open state and none of this
  // applies — there, every finished row folds, a failure included.
  const hasExpandableDetail =
    detailBeforeBlock !== null && detailBeforeBlock !== undefined
      ? true
      : isRunning ||
        block.activity.length > 0 ||
        hasActions ||
        isFail ||
        isOutcomeNotShown ||
        ownsOutcomeReason ||
        (detailAfterBlock !== null && detailAfterBlock !== undefined);
  const toggleable =
    hasExpandableDetail &&
    (expansion !== undefined ||
      isOk ||
      isOutcomeNotShown ||
      ownsOutcomeReason ||
      isVerifying ||
      isRanNeutral ||
      isStopped);
  useTick(isRunning);
  useFrameTick(hasActions && (replayingAction || elapsedReveal < totalMs));
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
  // The visible mark carries the state for sighted readers; this is the same
  // state as a word, without the duration statusText folds in — the row
  // already has an elapsed column.
  const stateWord = isOk
    ? "done"
    : isRunning
      ? "working"
      : isVerifying
        ? "verifying outcome"
        : isRanNeutral || isOutcomeNotShown
          ? "ran"
          : isFail
            ? "halted"
            : isStopped
              ? "stopped"
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
    if (expansion) {
      expansion.onToggle();
    } else {
      setUserOpen((v) => !(v === null ? defaultOpen : v));
    }
  };
  const collapsedExtras = (
    <>
      {!open && !expansion && isOk && block.activity.length > 0 ? (
        <div className="mt-0.5 text-[12px] leading-[1.5] text-muted-foreground">
          {block.activity[block.activity.length - 1]!.text}
        </div>
      ) : null}
      {!open && !expansion && isOutcomeNotShown ? (
        <div className="mt-0.5 text-[12px] leading-[1.5] text-amber-700 dark:text-amber-200/80">
          Outcome not confirmed — the run finished without showing the goal was
          met
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
      {detailBeforeBlock}
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
      {hasActions
        ? recordedActions!
            .slice(0, visibleActionCount)
            .map((action, i) => (
              <FRecordedActionRow
                key={action.actionId}
                action={action}
                revealing={replayingAction && i === revealedCount}
                flash={
                  i < revealedCount &&
                  elapsedReveal - offsets[i]! < FLASH_WINDOW_MS
                }
              />
            ))
        : null}
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
          <span className="text-[11px] font-bold text-amber-700 dark:text-amber-300">
            !
          </span>
          <div className="text-[12px] leading-[1.5] text-amber-700 dark:text-amber-200/90">
            {collapsedOutcomeReason ??
              "The step ran, but the run did not demonstrate the goal was met."}
          </div>
        </div>
      ) : null}
      {detailAfterBlock}
    </div>
  );

  if (flat) {
    return (
      <div className="flex flex-col">
        <FLogLine
          glyph={rowGlyph ?? <span className={accentText}>{stateGlyph}</span>}
          kindWord={rowKindWord}
          trailing={
            <>
              {elapsed ?? rowTrailing}
              {toggleable ? (open ? " ⌄" : " ›") : null}
            </>
          }
          onClick={onHeaderClick}
          expanded={toggleable && expansion ? open : undefined}
          title={`Highlight ${block.label} on canvas`}
        >
          {displayLabel}
          {rowGlyph === undefined ? null : (
            <span className={accentText} aria-hidden="true">
              {" "}
              {stateGlyph}
            </span>
          )}
          <span className="sr-only">{` · ${stateWord}`}</span>
          {rowDiffCounts === undefined ? null : (
            <span className="font-mono tabular-nums text-muted-foreground dark:text-slate-500">
              <span>{" · "}</span>
              <span>{`+${rowDiffCounts.added}`}</span>
              <span className="pl-1">{`−${rowDiffCounts.removed}`}</span>
            </span>
          )}
        </FLogLine>
        <div className="pl-[28px]">{collapsedExtras}</div>
        {open && hasExpandableDetail ? (
          <div className="pl-[28px]">{blockDetail}</div>
        ) : null}
      </div>
    );
  }

  return (
    <div className="flex flex-col">
      <button
        type="button"
        className={`flex w-full items-start gap-3 px-1 py-1 text-left ${
          toggleable ? "cursor-pointer" : "cursor-default"
        }`}
        aria-expanded={toggleable && expansion ? expansion.open : undefined}
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

export const COPILOT_ACK_LINES = [
  "Reading your request…",
  "Getting oriented…",
  "Sketching a plan…",
  "Lining up the steps…",
  "Thinking it through…",
] as const;

export const ACK_ROTATE_INTERVAL_MS = 3000;

// Fills the send→first-frame gap with a rotating shimmer so the build never starts on dead air.
// The first real narrative replaces it immediately; it never persists to history.
export function InstantAckPlaceholder() {
  // Random start so quick repeated sends (a gap near the rotation cadence)
  // don't always open on the same line.
  const [index, setIndex] = useState(() =>
    Math.floor(Math.random() * COPILOT_ACK_LINES.length),
  );
  useEffect(() => {
    const id = setInterval(
      () => setIndex((i) => (i + 1) % COPILOT_ACK_LINES.length),
      ACK_ROTATE_INTERVAL_MS,
    );
    return () => clearInterval(id);
  }, []);
  // Shimmer paints the text with a white gradient, which vanishes on the
  // near-white light surface — restrict it to dark, where the base
  // text-muted-foreground stays readable on its own.
  const isDark = useThemeAsDarkOrLight() === "dark";
  const shimmerRef = useShimmerText<HTMLSpanElement>(isDark);
  const line = COPILOT_ACK_LINES[index];
  return (
    <div className="flex items-center gap-3 px-1 py-1" role="status">
      <span className="sr-only">Copilot is working on your request…</span>
      <span
        className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-sky-400/60 bg-sky-500/15"
        aria-hidden="true"
      >
        <Spinner />
      </span>
      <span
        ref={shimmerRef}
        aria-hidden="true"
        className="text-[12.5px] font-medium text-muted-foreground"
      >
        {line}
      </span>
    </div>
  );
}

interface FActivityLogProps {
  turn: TurnNarrativeState;
  turnEnded: boolean;
  onBlockSelect?: (label: string) => void;
  interactionRef?: { current: string | null };
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
  const renderLine = (line: string, i: number, muted: boolean) => (
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
        <pre className="ml-5 overflow-x-auto whitespace-pre rounded border border-border/60 bg-muted/40 p-2 text-[11px] leading-[1.5]">
          {patchLines.map((line, i) => renderLine(line, i, false))}
        </pre>
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
            {patchLines.slice(0, 2).map((line, i) => renderLine(line, i, true))}
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

function FActivityLogRow({
  row,
  open,
  onToggle,
  diffOpen,
  diffPeek,
  onDiffToggle,
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
  turnEnded: boolean;
  onBlockSelect?: (label: string) => void;
  outcomeReasonFallback?: string | null;
  outcomeOwnerKey?: string | null;
}) {
  const last = row.entries[row.entries.length - 1];
  // A lone run card becomes the row itself, so the collapsed line keeps the
  // block's own verdict rather than the run tool's flag, which can disagree.
  // Unless the row is still calling and that block already finished: it is an
  // earlier run's card, and its verdict would read as this row's status.
  const only = row.blocks.length === 1 ? row.blocks[0] : undefined;
  const soloBlock =
    row.pending && only !== undefined && only.state !== "running"
      ? undefined
      : only;
  const exactFailure =
    last?.kind === "tool_result" && last.success === false ? last : null;
  const hasDetail =
    row.blocks.length > 0 ||
    row.entries.length > 1 ||
    row.codeDiffs.length > 0 ||
    row.reason !== null ||
    exactFailure !== null;
  // Body content is whatever the line does not already show: a solo block's
  // line is the card, so its tool entries stay represented by the block's own
  // evidence; otherwise the line is the last entry and the body carries the
  // ones before it.
  const bodyEntries = soloBlock ? [] : row.entries.slice(0, -1);
  const bodyFailures = bodyEntries.filter(
    (entry) => entry.kind === "tool_result" && entry.success === false,
  );
  const bodySteps = bodyEntries.filter(
    (entry) => entry.kind !== "tool_result" || entry.success !== false,
  );
  // The reason is a body child too: a row whose only detail is its reason
  // would otherwise render an empty container and hide the prose entirely.
  const bodyChildren = soloBlock
    ? 0
    : bodyEntries.length +
      row.blocks.length +
      row.codeDiffs.length +
      (row.reason === null ? 0 : 1) +
      (exactFailure === null ? 0 : 1);
  // The collapsed row carries the whole write's delta; per-block identity and
  // the patch itself live in the detail. A row folding two writes sums them,
  // since one line cannot name both blocks without becoming two.
  const rowAdded = row.codeDiffs.reduce((n, d) => n + d.added, 0);
  const rowRemoved = row.codeDiffs.reduce((n, d) => n + d.removed, 0);
  // Time-derived like the recorded-action reveal, so a hydrated row (no
  // arrival stamp) falls straight through to the full string on first render.
  const reasonShown =
    row.reason === null
      ? 0
      : revealedCharsAt(row.reason.length, Date.now() - (row.reasonAt ?? 0));
  const reasonRevealing =
    row.reason !== null && reasonShown < row.reason.length;
  useFrameTick(open && reasonRevealing);

  // A combined write/test row can carry both entries and blocks; prefer the
  // step count when there are several tool transitions to summarize.
  const foldedSummary =
    row.entries.length > 1 && (last?.attempts ?? 1) <= 1
      ? `\u00b7 ${row.entries.length} steps`
      : row.blocks.length > 0
        ? `\u00b7 ${row.blocks.length} ${row.blocks.length === 1 ? "block" : "blocks"}`
        : null;

  // While the work is still happening the trailing column is wall time since it
  // began; the row's own entry stamps only span what has been recorded, so a
  // row with one entry would read 0:00 for as long as the step took. Once the
  // row settles it reports the recorded span, and the tick stops with it.
  useTick(row.live);
  const rowElapsed = row.live
    ? liveElapsed(row.startedAt)
    : formatElapsed(row.startedAt, row.endedAt);

  const kindGlyph = row.kind === null ? null : ACTIVITY_KIND_GLYPH[row.kind];
  const kindWord = row.kind === null ? undefined : ACTIVITY_KIND_WORD[row.kind];

  const lineContent =
    soloBlock || last === undefined ? null : entryLine(last, row.label);
  // A mark reports an outcome, so only a step that returned can carry one — a
  // call still in flight has no outcome yet. Beyond that, a browse or write
  // step that worked says so in its own sentence, so only a run's result and
  // any failure earn a mark of their own.
  const settled = last !== undefined && last.kind === "tool_result";
  const mark =
    soloBlock || !settled
      ? null
      : last.success !== false && row.kind === "run"
        ? "✓"
        : null;

  const reasonNode =
    row.reason === null ? null : (
      <FSubRow
        glyph="✦"
        glyphClass="text-sky-700 dark:text-sky-300"
        italic
        muted
      >
        <span
          data-testid="copilot-reason"
          className={[
            "break-words [overflow-wrap:anywhere]",
            // The tail keeps the full string in flow so the row never grows,
            // while the cap prevents the reveal from moving later evidence.
            reasonRevealing
              ? "overflow-hidden [max-height:calc(4*1.55em)]"
              : "line-clamp-4",
          ].join(" ")}
        >
          {row.reason.slice(0, reasonShown)}
          <span
            aria-hidden="true"
            className={reasonRevealing ? "animate-pulse" : "opacity-0"}
          >
            {"\u258c"}
          </span>
          <span className="text-transparent">
            {row.reason.slice(reasonShown)}
          </span>
        </span>
      </FSubRow>
    );
  const diffNodes = row.codeDiffs.map((diff) => (
    <FCodeWriteDiff
      key={diff.label}
      diff={diff}
      open={diffOpen(diff.label)}
      peek={diffPeek(diff.label)}
      onToggle={() => onDiffToggle(diff.label)}
    />
  ));
  const rowFailures = row.entries.filter(
    (entry) => entry.kind === "tool_result" && entry.success === false,
  );

  const lineNode = soloBlock ? (
    <FBlockRun
      block={soloBlock}
      turnEnded={turnEnded}
      onSelect={onBlockSelect}
      outcomeReasonFallback={outcomeReasonFallback}
      ownsOutcomeNotConfirmed={blockIdentity(soloBlock) === outcomeOwnerKey}
      rowTitle={row.label}
      flat
      rowGlyph={kindGlyph}
      rowKindWord={kindWord}
      rowTrailing={rowElapsed}
      rowDiffCounts={
        row.codeDiffs.length === 0
          ? undefined
          : { added: rowAdded, removed: rowRemoved }
      }
      expansion={{ open, onToggle }}
      quietFailure
      detailBeforeBlock={
        row.reason !== null || diffNodes.length > 0 ? (
          <>
            {reasonNode}
            {diffNodes}
          </>
        ) : undefined
      }
      detailAfterBlock={
        rowFailures.length > 0 ? (
          <>
            {rowFailures.map((entry) => (
              <ActivityRow key={entry.id} entry={entry} />
            ))}
          </>
        ) : undefined
      }
    />
  ) : (
    <FLogLine
      glyph={kindGlyph}
      kindWord={kindWord}
      trailing={
        <>
          {rowElapsed}
          {hasDetail ? (open ? " ⌄" : " ›") : null}
        </>
      }
      onClick={hasDetail ? onToggle : undefined}
      expanded={hasDetail ? open : undefined}
    >
      {lineContent?.content}
      {mark === null ? null : (
        <span
          className={
            mark === "✓"
              ? "text-emerald-700 dark:text-emerald-300"
              : "text-rose-700 dark:text-rose-300"
          }
          aria-hidden="true"
        >
          {" "}
          {mark}
        </span>
      )}
      {!open && foldedSummary !== null ? (
        <span className="text-muted-foreground dark:text-slate-500">
          {" "}
          {foldedSummary}
        </span>
      ) : null}
      {row.codeDiffs.length === 0 ? null : (
        <span className="font-mono tabular-nums text-muted-foreground dark:text-slate-500">
          <span>{" \u00b7 "}</span>
          <span>{`+${rowAdded}`}</span>
          <span className="pl-1">{`\u2212${rowRemoved}`}</span>
        </span>
      )}
    </FLogLine>
  );

  return (
    <div className="flex flex-col">
      {lineNode}
      {open && bodyChildren > 0 ? (
        <div className="ml-[28px] flex flex-col gap-1 border-l border-border/60 py-1 pl-3">
          {reasonNode}
          {diffNodes}
          {bodySteps.map((entry) => (
            <ActivityRow key={entry.id} entry={entry} />
          ))}
          {(soloBlock ? [] : row.blocks).map((b) => (
            <FBlockRun
              key={b.workflowRunBlockId || b.label}
              block={b}
              turnEnded={turnEnded}
              onSelect={onBlockSelect}
              outcomeReasonFallback={outcomeReasonFallback}
              ownsOutcomeNotConfirmed={blockIdentity(b) === outcomeOwnerKey}
              flat
              quietFailure
            />
          ))}
          {bodyFailures.map((entry) => (
            <ActivityRow key={entry.id} entry={entry} />
          ))}
          {exactFailure === null ? null : <ActivityRow entry={exactFailure} />}
        </div>
      ) : null}
    </div>
  );
}

function FActivityLog({
  turn,
  turnEnded,
  onBlockSelect,
  interactionRef,
}: FActivityLogProps) {
  const outcomeReasonFallback = notConfirmedDisplayReason(turn);
  const outcomeOwnerKey = outcomeNotConfirmedOwnerKey(turn);
  const { rows, focusIndex } = useMemo(() => deriveActivityLog(turn), [turn]);
  // Signed, not a bare id set: a click on the live row has to be able to mean
  // "closed", or folding the active row would silently pin it open instead.
  const [override, setOverride] = useState<ReadonlyMap<string, boolean>>(
    () => new Map(),
  );
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
      row?.querySelector<HTMLButtonElement>("button")?.focus();
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
  const toggleDiff = useCallback(
    (rowId: string, label: string, open: boolean) => {
      lastInteractedRow.current = rowId;
      setOverride((prev) => {
        const next = new Map(prev);
        next.set(`diff:${rowId}:${label}`, !open);
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

  return (
    <div ref={logRef} className="flex flex-col gap-1.5">
      {rows.map((row, i) => {
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
        return (
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
                toggleDiff(row.id, label, diffOpen(label))
              }
              turnEnded={turnEnded}
              onBlockSelect={onBlockSelect}
              outcomeReasonFallback={outcomeReasonFallback}
              outcomeOwnerKey={outcomeOwnerKey}
            />
          </div>
        );
      })}
    </div>
  );
}

interface DetailViewProps {
  turn: TurnNarrativeState;
  onBlockSelect?: (label: string) => void;
  workingRowActive?: boolean;
  activityInteractionRef?: { current: string | null };
}

function DetailView({
  turn,
  onBlockSelect,
  workingRowActive,
  activityInteractionRef,
}: DetailViewProps) {
  const collapsedOutcomeReason = notConfirmedDisplayReason(turn);
  const outcomeOwnerKey = outcomeNotConfirmedOwnerKey(turn);
  const hasBlocks = turn.blocks.length > 0;
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

  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex flex-col gap-2.5">
        {showChecklist ? (
          <FActivityLog
            key={turn.turnId ?? ""}
            turn={turn}
            turnEnded={turn.terminal !== null}
            onBlockSelect={onBlockSelect}
            interactionRef={activityInteractionRef}
          />
        ) : showDesign ? (
          <FDesignRow
            done={!designOpen}
            blockLabels={turn.draft?.blockLabels ?? []}
            activity={turn.designActivity}
          />
        ) : preBlockNarration.length > 0 ? (
          preBlockNarration.map((e) => (
            <FProse key={e.id} text={e.text} muted italic />
          ))
        ) : null}

        {!showChecklist && hasBlocks ? (
          <div className="flex flex-col gap-1">
            {turn.blocks.map((b) => (
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
          <div className="pl-9 text-[12px] italic text-muted-foreground dark:text-slate-500">
            Working…
          </div>
        ) : null}

        {collapsedOutcomeReason !== null && outcomeOwnerKey === null ? (
          <div className="flex items-start gap-2 rounded-md border border-amber-400/30 bg-amber-500/10 px-2.5 py-1.5">
            <span className="text-[11px] font-bold text-amber-700 dark:text-amber-300">
              !
            </span>
            <div className="text-[12px] leading-[1.5] text-amber-700 dark:text-amber-200/90">
              <span className="font-semibold">Outcome not confirmed</span>
              {` — ${truncateOutcomeReason(collapsedOutcomeReason)}`}
            </div>
          </div>
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
              "text-[13px] leading-[1.55]",
              awaitsUserInput(turn)
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
}

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
  if (
    awaitsUserInput(turn) ||
    turn.responseKind === "clarify" ||
    turn.responseType === "ASK_QUESTION"
  ) {
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
}: NarrativeViewProps) {
  const proseTone = terminalProseTone(turn);
  const proseText = humanizeJudgeText(terminalNarrativeText(turn));
  const activityInteractionRef = useRef<string | null>(null);

  if (proseTone !== null && proseText) {
    return (
      <TerminalProse
        text={proseText}
        tone={proseTone}
        arrivedAt={turn.endedAt}
      />
    );
  }

  return (
    <DetailView
      turn={turn}
      onBlockSelect={onBlockSelect}
      workingRowActive={workingRowActive}
      activityInteractionRef={activityInteractionRef}
    />
  );
}
