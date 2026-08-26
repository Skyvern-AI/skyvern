import {
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";

import {
  buildRevealOffsets,
  revealedCharsAt,
  revealedCountAt,
} from "./actionReveal";
import { humanizeBlockLabel } from "./blockLabel";
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
  TurnSummary,
  computeTurnSummary,
  formatElapsed,
  humanizeJudgeText,
  isBlockOk,
  isInterimOutcome,
  latestBlocksByLabel,
  notConfirmedOutcome,
  parseUtcIsoMs,
  toolActivityDisplayLabel,
} from "./narrativeState";
import { useShimmerText } from "../workflowRun/useShimmerText";
import { useThemeAsDarkOrLight } from "../../../components/useThemeAsDarkOrLight";

// Row flashes green/red for 600ms once revealed — must match the tailwind
// copilot-row-flash-* animation duration.
const FLASH_WINDOW_MS = 600;
const OUTCOME_REASON_PREVIEW_LIMIT = 140;

function normalizeOutcomeReason(
  reason: string | null | undefined,
): string | null {
  const trimmed = reason?.trim();
  if (!trimmed) return null;
  const humanized = humanizeJudgeText(trimmed);
  return humanized.length > 0 ? humanized : null;
}

function normalizeOutcomeReasonSearchText(
  text: string | null | undefined,
): string {
  const normalized = normalizeOutcomeReason(text);
  if (!normalized) return "";
  return normalized
    .toLowerCase()
    .replace(/\s+/g, " ")
    .replace(/[.,!?;:]+$/g, "")
    .trim();
}

function truncateOutcomeReason(reason: string): string {
  if (reason.length <= OUTCOME_REASON_PREVIEW_LIMIT) return reason;
  const slice = reason.slice(0, OUTCOME_REASON_PREVIEW_LIMIT - 3).trimEnd();
  return `${slice}...`;
}

function notConfirmedDisplayReason(turn: TurnNarrativeState): string | null {
  return normalizeOutcomeReason(notConfirmedOutcome(turn)?.displayReason);
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
  return {
    content: (
      <>
        <span className={ok ? undefined : "text-rose-700 dark:text-rose-200"}>
          {ok ? (title ?? entry.text) : entry.text}
        </span>
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
// narration moves a character every 14ms, and buildRevealOffsets scales a long
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
  uxV1?: boolean;
  outcomeReasonFallback?: string | null;
  // Narrator title for the row this card heads. The card's own status text and
  // the rollup's block lists still name the block, so the title replaces only
  // the label here.
  rowTitle?: string | null;
  // Inside the activity log the card sheds its puck for the shared row grid,
  // so a block does not read as a different species from the steps around it.
  flat?: boolean;
  // Supplied when the log's row already names the kind of work. The block's
  // own state then rides inline as a mark instead of taking the gutter.
  rowGlyph?: React.ReactNode;
  rowKindWord?: string;
  rowTrailing?: React.ReactNode;
  // When the activity log owns this card's row, open/closed comes from there
  // so the row and the card never disagree about a single click.
  expansion?: { open: boolean; onToggle: () => void };
}

function FBlockRun({
  block,
  turnEnded,
  onSelect,
  uxV1,
  outcomeReasonFallback,
  rowTitle,
  flat,
  rowGlyph,
  rowKindWord,
  rowTrailing,
  expansion,
}: FBlockRunProps) {
  const displayLabel =
    rowTitle ?? (uxV1 ? humanizeBlockLabel(block.label) : block.label);
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
  const toggleable =
    expansion !== undefined ||
    isOk ||
    isOutcomeNotShown ||
    isVerifying ||
    isRanNeutral ||
    isStopped;
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
  const collapsedOutcomeReason = isOutcomeNotShown
    ? normalizeOutcomeReason(block.outcomeReason ?? outcomeReasonFallback)
    : null;

  const onHeaderClick = () => {
    onSelect?.(block.label);
    if (expansion) {
      expansion.onToggle();
    } else if (toggleable) {
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
            {block.activity.find((e) => e.kind === "tool_result")?.text ??
              "Halted — see run details."}
          </div>
        </div>
      ) : null}
      {isOutcomeNotShown ? (
        <div className="mt-1 flex items-start gap-2 rounded-md border border-amber-400/30 bg-amber-500/10 px-2.5 py-1.5">
          <span className="text-[11px] font-bold text-amber-700 dark:text-amber-300">
            !
          </span>
          <div className="text-[12px] leading-[1.5] text-amber-700 dark:text-amber-200/90">
            {normalizeOutcomeReason(block.outcomeReason) ??
              "The step ran, but the run did not demonstrate the goal was met."}
          </div>
        </div>
      ) : null}
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
          expanded={expansion ? open : undefined}
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
        </FLogLine>
        <div className="pl-[28px]">{collapsedExtras}</div>
        {open ? <div className="pl-[28px]">{blockDetail}</div> : null}
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
        aria-expanded={expansion ? expansion.open : undefined}
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
              className={
                uxV1
                  ? "text-[12.5px] font-semibold text-foreground"
                  : "font-mono text-[12.5px] font-semibold text-foreground"
              }
              title={uxV1 ? block.label : undefined}
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

      {open ? <div className="ml-9">{blockDetail}</div> : null}
    </div>
  );
}

interface FDesignRowProps {
  done: boolean;
  blockLabels: string[];
  activity: ActivityEntry[];
  uxV1?: boolean;
}

function FDesignRow({ done, blockLabels, activity, uxV1 }: FDesignRowProps) {
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
              <span
                className={
                  uxV1 ? "text-foreground" : "font-mono text-foreground"
                }
                title={uxV1 ? label : undefined}
              >
                {uxV1 ? humanizeBlockLabel(label) : label}
              </span>
            </FSubRow>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function FWorkingHeader() {
  return (
    <div className="flex items-center gap-2 px-1 py-1">
      <Spinner />
      <span className="text-[12.5px] font-semibold text-foreground">
        Working…
      </span>
      <span className="text-[11px] text-muted-foreground">
        · building your workflow
      </span>
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
  uxV1?: boolean;
}

// The kind gutter sits to the left of ActivityRow's own status column, so a
// row reads <kind> <status> <text> and neither signal displaces the other.
// The counts line sits outside the row's own expand button, so its `view diff`
// control is never a button nested inside another button.
function FCodeWriteDiff({
  diff,
  open,
  onToggle,
  uxV1,
}: {
  diff: CodeWriteDiff;
  open: boolean;
  onToggle: () => void;
  uxV1?: boolean;
}) {
  return (
    <div className="flex flex-col">
      <FSubRow glyph="±" glyphClass="text-sky-700 dark:text-sky-300">
        <span
          className={uxV1 ? "text-foreground" : "font-mono text-foreground"}
          title={diff.label}
        >
          {uxV1 ? humanizeBlockLabel(diff.label) : diff.label}
        </span>
        <span className="pl-1.5 font-mono text-emerald-700 dark:text-emerald-300">
          {`+${diff.added}`}
        </span>
        <span className="pl-1 font-mono text-rose-700 dark:text-rose-300">
          {`−${diff.removed}`}
        </span>
        {/* A disabled control receives no pointer events, so the reason why it is
            dead has to hang on something that does. */}
        <span
          title={
            diff.patchDropped
              ? "The diff was too large to keep, so only its line counts were saved."
              : undefined
          }
        >
          <button
            type="button"
            className="pl-2 text-muted-foreground underline-offset-2 hover:underline disabled:no-underline disabled:opacity-50"
            disabled={diff.patch === undefined}
            aria-expanded={open}
            onClick={onToggle}
          >
            {open ? "hide diff" : "view diff"}
          </button>
        </span>
      </FSubRow>
      {open && diff.patch !== undefined ? (
        <pre className="ml-5 overflow-x-auto whitespace-pre rounded border border-border/60 bg-muted/40 p-2 text-[11px] leading-[1.5]">
          {diff.patch.split("\n").map((line, i) => (
            <div
              key={`${i}-${line}`}
              className={
                line.startsWith("+")
                  ? "text-emerald-700 dark:text-emerald-300"
                  : line.startsWith("-")
                    ? "text-rose-700 dark:text-rose-300"
                    : "text-muted-foreground"
              }
            >
              {line}
            </div>
          ))}
        </pre>
      ) : null}
    </div>
  );
}

function FActivityLogRow({
  row,
  open,
  onToggle,
  diffOpen,
  onDiffToggle,
  turnEnded,
  onBlockSelect,
  uxV1,
  outcomeReasonFallback,
}: {
  row: ActivityRowModel;
  open: boolean;
  onToggle: () => void;
  diffOpen: (label: string) => boolean;
  onDiffToggle: (label: string) => void;
  turnEnded: boolean;
  onBlockSelect?: (label: string) => void;
  uxV1?: boolean;
  outcomeReasonFallback?: string | null;
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
  const hasDetail =
    row.blocks.length > 0 ||
    row.entries.length > 1 ||
    row.codeDiffs.length > 0 ||
    row.reason !== null;
  // Body content is whatever the line does not already show: a solo block's
  // line is the card, so every entry is still unrendered; otherwise the line
  // is the last entry and the body carries the ones before it.
  const bodyEntries = soloBlock ? row.entries : row.entries.slice(0, -1);
  // The reason is a body child too: a row whose only detail is its reason
  // would otherwise render an empty container and hide the prose entirely.
  const bodyChildren =
    bodyEntries.length +
    (soloBlock ? 0 : row.blocks.length) +
    row.codeDiffs.length +
    (row.reason === null ? 0 : 1);
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

  // Only a browse row accumulates entries, and only a run row carries blocks,
  // so these two counts can never both apply to one row.
  const foldedSummary =
    row.entries.length > 1
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
      : last.success === false
        ? "✕"
        : row.kind === "run"
          ? "✓"
          : null;

  const lineNode = soloBlock ? (
    <FBlockRun
      block={soloBlock}
      turnEnded={turnEnded}
      onSelect={onBlockSelect}
      uxV1={uxV1}
      outcomeReasonFallback={outcomeReasonFallback}
      rowTitle={row.label}
      flat
      rowGlyph={kindGlyph}
      rowKindWord={kindWord}
      rowTrailing={rowElapsed}
      expansion={{ open, onToggle }}
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
        <span className="font-mono tabular-nums">
          <span className="text-muted-foreground dark:text-slate-500">
            {" \u00b7 "}
          </span>
          <span className="text-emerald-700 dark:text-emerald-300">{`+${rowAdded}`}</span>
          <span className="pl-1 text-rose-700 dark:text-rose-300">{`\u2212${rowRemoved}`}</span>
        </span>
      )}
    </FLogLine>
  );

  return (
    <div className="flex flex-col">
      {lineNode}
      {open && bodyChildren > 0 ? (
        <div className="ml-[28px] flex flex-col gap-1 border-l border-border/60 py-1 pl-3">
          {row.codeDiffs.map((diff) => (
            <FCodeWriteDiff
              key={diff.label}
              diff={diff}
              open={diffOpen(diff.label)}
              onToggle={() => onDiffToggle(diff.label)}
              uxV1={uxV1}
            />
          ))}
          {row.reason === null ? null : (
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
                  // The tail keeps the full string in flow so the row never
                  // grows, but that also makes the clamp paint its ellipsis on
                  // line four from the first frame — stranded in the
                  // transparent region while the reveal is still on line one.
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
          )}
          {bodyEntries.map((entry) => (
            <ActivityRow key={entry.id} entry={entry} />
          ))}
          {(soloBlock ? [] : row.blocks).map((b) => (
            <FBlockRun
              key={b.workflowRunBlockId || b.label}
              block={b}
              turnEnded={turnEnded}
              onSelect={onBlockSelect}
              uxV1={uxV1}
              outcomeReasonFallback={outcomeReasonFallback}
              flat
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

interface FActivityLogProps {
  turn: TurnNarrativeState;
  turnEnded: boolean;
  onBlockSelect?: (label: string) => void;
  uxV1?: boolean;
}

function FActivityLog({
  turn,
  turnEnded,
  onBlockSelect,
  uxV1,
}: FActivityLogProps) {
  const outcomeReasonFallback = notConfirmedDisplayReason(turn);
  const { rows, focusIndex } = useMemo(() => deriveActivityLog(turn), [turn]);
  // Signed, not a bare id set: a click on the live row has to be able to mean
  // "closed", or folding the active row would silently pin it open instead.
  const [override, setOverride] = useState<ReadonlyMap<string, boolean>>(
    () => new Map(),
  );
  const toggle = useCallback((id: string, open: boolean) => {
    setOverride((prev) => new Map(prev).set(id, !open));
  }, []);
  // Every write's patch stays open for the rest of the turn rather than closing when a
  // newer write lands: a patch that vanishes mid-read is worse than a longer log. At Done
  // they all collapse to their counts, with the body behind `view diff`.
  const writeDiffsOpen = !turnEnded;

  return (
    <div className="flex flex-col gap-1.5">
      {rows.map((row, i) => {
        const showsWriteDiff = writeDiffsOpen && row.codeDiffs.length > 0;
        const open =
          override.get(row.id) ?? (i === focusIndex || showsWriteDiff);
        const diffOpen = (label: string) =>
          override.get(`diff:${row.id}:${label}`) ?? showsWriteDiff;
        return (
          <FActivityLogRow
            key={row.id}
            row={row}
            open={open}
            onToggle={() => toggle(row.id, open)}
            diffOpen={diffOpen}
            onDiffToggle={(label) =>
              toggle(`diff:${row.id}:${label}`, diffOpen(label))
            }
            turnEnded={turnEnded}
            onBlockSelect={onBlockSelect}
            uxV1={uxV1}
            outcomeReasonFallback={outcomeReasonFallback}
          />
        );
      })}
    </div>
  );
}

function accentBg(accent: TurnSummary["accent"]): string {
  if (accent === "fail") {
    return "border-rose-400/60 bg-rose-500/15 text-rose-700 dark:text-rose-300";
  }
  if (accent === "warn") {
    return "border-amber-400/60 bg-amber-500/15 text-amber-700 dark:text-amber-300";
  }
  if (accent === "qa") {
    return "border-sky-400/60 bg-sky-500/15 text-sky-700 dark:text-sky-300";
  }
  return "border-emerald-400/60 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300";
}

interface TurnHeadProps {
  summary: TurnSummary;
  expanded: boolean;
  onClick?: () => void;
  subtitle?: ReactNode;
}

function TurnHead({ summary, expanded, onClick, subtitle }: TurnHeadProps) {
  const expandable = Boolean(onClick);
  const headClass = "flex w-full items-start gap-3 px-3.5 py-3 text-left";
  const body = (
    <>
      <span
        className={`flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-full border text-[12px] font-bold ${accentBg(
          summary.accent,
        )}`}
        aria-hidden="true"
      >
        {summary.glyph}
      </span>
      <div className="min-w-0 flex-1">
        <div className="mb-0.5 flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span className="text-[14px] font-semibold tracking-tight text-foreground">
            {summary.headline}
          </span>
          {summary.stats.length ? (
            <span className="text-[11.5px] text-muted-foreground">
              {summary.stats.join(" · ")}
            </span>
          ) : null}
        </div>
        {subtitle}
      </div>
      {expandable ? (
        <span
          className={`mt-1 shrink-0 text-[14px] text-muted-foreground transition-transform dark:text-slate-500 ${
            expanded ? "rotate-90" : ""
          }`}
          aria-hidden="true"
        >
          ›
        </span>
      ) : null}
    </>
  );

  if (!expandable) {
    return <div className={headClass}>{body}</div>;
  }

  return (
    <button
      type="button"
      onClick={onClick}
      aria-expanded={expanded}
      className={headClass}
    >
      {body}
    </button>
  );
}

interface RollupCardProps {
  turn: TurnNarrativeState;
  summary: TurnSummary;
  onExpand: () => void;
  onBlockSelect?: (label: string) => void;
  uxV1?: boolean;
}

function RollupCard({
  turn,
  summary,
  onExpand,
  onBlockSelect,
  uxV1,
}: RollupCardProps) {
  // The backend appends the judge's verdict to the closing message, so it needs the
  // same display-layer rewrite the outcome reason gets.
  const closing = humanizeJudgeText(
    turn.narrativeSummary?.trim() || turn.terminalMessage?.trim() || "",
  );
  const collapsedOutcomeReason = notConfirmedDisplayReason(turn);
  const truncatedOutcomeReason = collapsedOutcomeReason
    ? truncateOutcomeReason(collapsedOutcomeReason)
    : null;
  const normalizedClosing = normalizeOutcomeReasonSearchText(closing);
  // Normalizing the truncated preview (its trailing "..." strips as punctuation)
  // makes the containment check a prefix match, so closings carrying either the
  // full reason or a truncated form of it both suppress the appended segment.
  const normalizedOutcomeReason = normalizeOutcomeReasonSearchText(
    truncatedOutcomeReason,
  );
  const shouldAppendOutcomeReason =
    normalizedOutcomeReason.length > 0 &&
    !normalizedClosing.includes(normalizedOutcomeReason);
  const outcomeReasonSubtitle = shouldAppendOutcomeReason
    ? `Outcome not confirmed: ${truncatedOutcomeReason!}`
    : "";
  const subtitle = [closing, outcomeReasonSubtitle].filter(Boolean).join(" · ");
  const rollupBlocks = latestBlocksByLabel(turn.blocks);
  const completed = rollupBlocks.filter((b) => isBlockOk(b));
  const failed = rollupBlocks.filter((b) => b.state === "failed");
  const stopped = rollupBlocks.filter((b) => b.state === "stopped");
  const showChecklist = Boolean(uxV1) && showPhaseChecklist(turn);
  // The log holds every block, but a non-solo run row renders its cards in the
  // body, which is collapsed once the turn ends — so the log names a block on
  // screen only in the one-block case. These lists stay until a run row carries
  // its blocks' outcomes in the collapsed line.
  const showCommit = !summary.isQA && completed.length > 0;
  const showHalted = failed.length > 0;
  // Expand only earns a chevron when DetailView adds content beyond the head's
  // message — a pure ask (no scouting) re-renders the same text, so no chevron.
  const hasExpandableDetail =
    showChecklist ||
    turn.blocks.length > 0 ||
    (turn.designStarted && (turn.draft?.blockCount ?? 0) > 0) ||
    turn.designActivity.some((e) => e.kind === "narration");

  return (
    <div className="overflow-hidden rounded-xl border border-border/60 bg-slate-elevation2">
      <TurnHead
        summary={summary}
        expanded={false}
        onClick={hasExpandableDetail ? onExpand : undefined}
        subtitle={
          subtitle ? (
            <div
              className={`mt-0.5 text-[12.5px] leading-[1.5] ${
                summary.isFail && !summary.isStoppedWithDraft
                  ? "text-rose-700 dark:text-rose-200/90"
                  : "text-muted-foreground"
              }`}
            >
              {subtitle}
            </div>
          ) : null
        }
      />

      {showChecklist ? (
        <div className="border-t border-white/5 px-3.5 py-2">
          <FActivityLog
            key={turn.turnId ?? ""}
            turn={turn}
            turnEnded
            onBlockSelect={onBlockSelect}
            uxV1={uxV1}
          />
        </div>
      ) : null}

      {showCommit ? (
        <div className="border-t border-white/5 pb-3 pl-[52px] pr-3.5 pt-2.5">
          <div className="mb-1.5 text-[10px] font-bold uppercase tracking-[.06em] text-muted-foreground dark:text-slate-500">
            What changed
          </div>
          <ul className="m-0 flex list-none flex-col gap-1 p-0">
            {completed.map((b) => {
              const palette = paletteFor(b.blockType);
              return (
                <li
                  key={b.label}
                  className="flex items-baseline gap-1.5 text-[12px] leading-[1.5] text-foreground dark:text-slate-200"
                >
                  <span
                    className={`w-3.5 shrink-0 text-center text-[11px] font-bold ${palette.fg}`}
                    aria-hidden="true"
                  >
                    {palette.glyph}
                  </span>
                  <span
                    className={
                      uxV1
                        ? "text-[11px] text-muted-foreground"
                        : "font-mono text-[11px] text-muted-foreground"
                    }
                    title={uxV1 ? b.label : undefined}
                  >
                    {uxV1 ? humanizeBlockLabel(b.label) : b.label}
                  </span>
                  <span className="text-slate-600">·</span>
                  <span className="text-[11.5px] text-foreground dark:text-slate-200">
                    {b.blockType}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      {showHalted ? (
        <div className="border-t border-white/5 pb-3 pl-[52px] pr-3.5 pt-2.5">
          <div className="mb-1.5 text-[10px] font-bold uppercase tracking-[.06em] text-rose-700 dark:text-rose-400">
            Halted
          </div>
          <ul className="m-0 flex list-none flex-col gap-1 p-0">
            {failed.map((b) => (
              <li
                key={b.label}
                className="flex items-baseline gap-1.5 text-[12px] leading-[1.5] text-rose-700 dark:text-rose-200"
              >
                <span
                  className="w-3.5 shrink-0 text-center text-[11px] font-bold text-rose-700 dark:text-rose-300"
                  aria-hidden="true"
                >
                  ✕
                </span>
                <span
                  className={
                    uxV1
                      ? "text-[11px] text-rose-700 dark:text-rose-300/80"
                      : "font-mono text-[11px] text-rose-700 dark:text-rose-300/80"
                  }
                  title={uxV1 ? b.label : undefined}
                >
                  {uxV1 ? humanizeBlockLabel(b.label) : b.label}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {stopped.length > 0 ? (
        <div className="border-t border-white/5 pb-3 pl-[52px] pr-3.5 pt-2.5">
          <div className="mb-1.5 text-[10px] font-bold uppercase tracking-[.06em] text-muted-foreground">
            Stopped
          </div>
          <ul className="m-0 flex list-none flex-col gap-1 p-0">
            {stopped.map((b) => (
              <li
                key={b.label}
                className="flex items-baseline gap-1.5 text-[12px] leading-[1.5] text-tertiary-foreground"
              >
                <span
                  className="w-3.5 shrink-0 text-center text-[11px] font-bold text-muted-foreground"
                  aria-hidden="true"
                >
                  ■
                </span>
                <span
                  className={
                    uxV1
                      ? "text-[11px] text-muted-foreground"
                      : "font-mono text-[11px] text-muted-foreground"
                  }
                  title={uxV1 ? b.label : undefined}
                >
                  {uxV1 ? humanizeBlockLabel(b.label) : b.label}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

interface DetailViewProps {
  turn: TurnNarrativeState;
  onCollapse: (() => void) | null;
  onBlockSelect?: (label: string) => void;
  uxV1?: boolean;
  workingRowActive?: boolean;
}

function DetailView({
  turn,
  onCollapse,
  onBlockSelect,
  uxV1,
  workingRowActive,
}: DetailViewProps) {
  const collapsedOutcomeReason = notConfirmedDisplayReason(turn);
  const hasBlocks = turn.blocks.length > 0;
  const designStarted = turn.designStarted;
  const designOpen = designStarted && !turn.designEnded;
  // Hide the "Designed the workflow" cluster on terminal turns that produced
  // no draft (Q&A / clarify / refuse routes occasionally emit design_start
  // before the agent decides not to build). Live turns still surface it so a
  // long design phase isn't silently invisible.
  const hasDraft = (turn.draft?.blockCount ?? 0) > 0;
  const showDesign = designStarted && (hasDraft || hasBlocks || !turn.terminal);
  const showChecklist = Boolean(uxV1) && showPhaseChecklist(turn);
  const preBlockNarration = turn.designActivity.filter(
    (e) => e.kind === "narration",
  );

  return (
    <div className="flex flex-col gap-2.5">
      {onCollapse ? (
        <button
          type="button"
          onClick={onCollapse}
          aria-label="Collapse turn"
          className="flex w-full items-center justify-end gap-1.5 px-3.5 py-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground hover:text-tertiary-foreground dark:text-slate-500"
        >
          <span>Collapse</span>
          <span aria-hidden="true" className="rotate-90 text-[13px]">
            ›
          </span>
        </button>
      ) : null}

      {showChecklist ? (
        <>
          {turn.terminal === null ? <FWorkingHeader /> : null}
          <FActivityLog
            key={turn.turnId ?? ""}
            turn={turn}
            turnEnded={turn.terminal !== null}
            onBlockSelect={onBlockSelect}
            uxV1={uxV1}
          />
        </>
      ) : showDesign ? (
        <FDesignRow
          done={!designOpen}
          blockLabels={turn.draft?.blockLabels ?? []}
          activity={turn.designActivity}
          uxV1={uxV1}
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
              uxV1={uxV1}
              outcomeReasonFallback={collapsedOutcomeReason}
            />
          ))}
        </div>
      ) : null}

      {!hasBlocks && !designStarted && !turn.terminal && !workingRowActive ? (
        <div className="pl-9 text-[12px] italic text-muted-foreground dark:text-slate-500">
          Working…
        </div>
      ) : null}

      {turn.terminal && (turn.narrativeSummary || turn.terminalMessage) ? (
        <div className="whitespace-pre-wrap pl-9 pr-8 text-[13px] leading-[1.55] text-foreground dark:text-slate-200">
          {humanizeJudgeText(
            turn.narrativeSummary?.trim() || turn.terminalMessage?.trim() || "",
          )}
        </div>
      ) : null}
    </div>
  );
}

interface NarrativeViewProps {
  turn: TurnNarrativeState;
  onBlockSelect?: (blockLabel: string) => void;
  uxV1?: boolean;
  workingRowActive?: boolean;
}

export function NarrativeView({
  turn,
  onBlockSelect,
  uxV1,
  workingRowActive,
}: NarrativeViewProps) {
  const summary = useMemo(
    () => computeTurnSummary(turn, { uxV1 }),
    [turn, uxV1],
  );
  const isInFlight = turn.terminal === null;
  const isComplete = !isInFlight;
  const [userRolled, setUserRolled] = useState<boolean | null>(null);
  const rolled = userRolled === null ? isComplete : userRolled;

  if (rolled && isComplete) {
    return (
      <RollupCard
        turn={turn}
        summary={summary}
        onExpand={() => setUserRolled(false)}
        onBlockSelect={onBlockSelect}
        uxV1={uxV1}
      />
    );
  }

  return (
    <DetailView
      turn={turn}
      onCollapse={isComplete ? () => setUserRolled(true) : null}
      onBlockSelect={onBlockSelect}
      uxV1={uxV1}
      workingRowActive={workingRowActive}
    />
  );
}
