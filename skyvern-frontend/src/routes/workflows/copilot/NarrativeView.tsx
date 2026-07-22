import { type ReactNode, useEffect, useMemo, useState } from "react";

import { buildRevealOffsets, revealedCountAt } from "./actionReveal";
import { humanizeBlockLabel } from "./blockLabel";
import {
  CopilotPhaseId,
  PhaseStatus,
  derivePhases,
  showPhaseChecklist,
} from "./copilotPhases";
import {
  ActivityEntry,
  BlockState,
  RecordedActionSummary,
  TurnNarrativeState,
  TurnSummary,
  computeTurnSummary,
  condenseActivityEntries,
  effectiveMode,
  formatElapsed,
  isBlockOk,
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
  return trimmed && trimmed.length > 0 ? trimmed : null;
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

function ActivityRow({ entry }: { entry: ActivityEntry }) {
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
}

function FBlockRun({
  block,
  turnEnded,
  onSelect,
  uxV1,
  outcomeReasonFallback,
}: FBlockRunProps) {
  const displayLabel = uxV1 ? humanizeBlockLabel(block.label) : block.label;
  const palette = paletteFor(block.blockType);
  const isRunning = block.state === "running";
  const isCompleted = block.state === "completed";
  const isEvaluating = isCompleted && block.outcome === "evaluating";
  // A row stuck in `evaluating` at turn end (dropped stream) renders the
  // neutral "ran" treatment — never the live verifying beat, never green.
  const isVerifying = isEvaluating && !turnEnded;
  const isRanNeutral = isEvaluating && turnEnded;
  const isOutcomeNotShown = isCompleted && block.outcome === "not_demonstrated";
  const isOk = isBlockOk(block);
  const isFail = block.state === "failed";
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
  const open = userOpen === null ? defaultOpen : userOpen;
  const toggleable = isOk || isOutcomeNotShown || isVerifying || isRanNeutral;
  useTick(isRunning);
  useTick(hasActions && (replayingAction || elapsedReveal < totalMs), 150);
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
            : isDraft
              ? "drafted"
              : "queued";
  const collapsedOutcomeReason = isOutcomeNotShown
    ? normalizeOutcomeReason(block.outcomeReason ?? outcomeReasonFallback)
    : null;

  return (
    <div className="flex flex-col">
      <button
        type="button"
        className={`flex w-full items-start gap-3 px-1 py-1 text-left ${
          toggleable ? "cursor-pointer" : "cursor-default"
        }`}
        onClick={() => {
          onSelect?.(block.label);
          if (toggleable) {
            setUserOpen((v) => !(v === null ? defaultOpen : v));
          }
        }}
        title={`Highlight ${block.label} on canvas`}
      >
        <span
          className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-[11px] font-bold ${accentBorder} ${accentText} ${puckBg}`}
          aria-hidden="true"
        >
          {isOk ? (
            "✓"
          ) : isOutcomeNotShown ? (
            "!"
          ) : isVerifying ? (
            "…"
          ) : isFail ? (
            "✕"
          ) : isRunning ? (
            <Spinner />
          ) : (
            palette.glyph
          )}
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
          {!open && isOk && block.activity.length > 0 ? (
            <div className="mt-0.5 text-[12px] leading-[1.5] text-muted-foreground">
              {block.activity[block.activity.length - 1]!.text}
            </div>
          ) : null}
          {!open && isOutcomeNotShown ? (
            <div className="mt-0.5 text-[12px] leading-[1.5] text-amber-700 dark:text-amber-200/80">
              Outcome not confirmed — the run finished without showing the goal
              was met
              {collapsedOutcomeReason
                ? `: ${truncateOutcomeReason(collapsedOutcomeReason)}`
                : "."}
            </div>
          ) : null}
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

      {open ? (
        <div className="ml-9 flex flex-col gap-1.5 border-l border-border/60 py-1.5 pl-3">
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
                {block.outcomeReason ??
                  "The step ran, but the run did not demonstrate the goal was met."}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
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

function phaseGlyph(status: PhaseStatus): ReactNode {
  switch (status) {
    case "done":
      return "✓";
    case "fail":
      return "✕";
    case "active":
      return <Spinner />;
    default:
      return "○";
  }
}

function phasePuckClasses(status: PhaseStatus): string {
  switch (status) {
    case "done":
      return "border-emerald-400/60 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300";
    case "fail":
      return "border-rose-400/60 bg-rose-500/15 text-rose-700 dark:text-rose-300";
    case "active":
      return "border-blue-400/60 bg-blue-500/15 text-blue-700 dark:text-blue-300";
    default:
      return "border-slate-500/60 bg-slate-elevation3 text-slate-600";
  }
}

function phaseLabelClasses(status: PhaseStatus): string {
  switch (status) {
    case "active":
      return "font-semibold text-foreground";
    case "fail":
      return "text-rose-700 dark:text-rose-300";
    case "pending":
    case "notrun":
      return "text-muted-foreground dark:text-slate-500";
    default:
      return "text-muted-foreground";
  }
}

// Status is otherwise conveyed only via an aria-hidden glyph/color — this
// gives screen-reader users the same information as sighted users.
function phaseStatusWord(status: PhaseStatus): string {
  switch (status) {
    case "active":
      return "Active";
    case "done":
      return "Done";
    case "fail":
      return "Failed";
    case "stopped":
      return "Stopped";
    case "notrun":
      return "Not run";
    default:
      return "Pending";
  }
}

// While Draft is active its stream is necessarily empty (the LLM is writing
// code, no frames arrive) — one shimmered placeholder row fills that gap,
// naming the redraft iteration once a prior verify failed.
function DraftPlaceholderNote({ turn }: { turn: TurnNarrativeState }) {
  const shimmerRef = useShimmerText<HTMLSpanElement>(true);
  const priorFailedVerdict =
    turn.lastRunOutcome?.verdict === "not_demonstrated" ||
    turn.lastRunOutcome?.verdict === "not_evaluated";
  const text = priorFailedVerdict
    ? `Draft v${turn.authoringCount + 1} — revising after failed verify: ${
        turn.lastRunOutcome?.displayReason ?? "outcome not confirmed"
      }`
    : "Writing the workflow code…";
  return (
    <FSubRow glyph="▸" glyphClass="text-muted-foreground">
      <span
        ref={shimmerRef}
        title={text}
        className="block truncate text-muted-foreground"
      >
        {text}
      </span>
    </FSubRow>
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

interface FPhaseChecklistProps {
  turn: TurnNarrativeState;
  turnEnded: boolean;
  onBlockSelect?: (label: string) => void;
  uxV1?: boolean;
}

function FPhaseChecklist({
  turn,
  turnEnded,
  onBlockSelect,
  uxV1,
}: FPhaseChecklistProps) {
  const collapsedOutcomeReason = notConfirmedDisplayReason(turn);
  const rows = useMemo(() => derivePhases(turn), [turn]);
  const condensedBlocks = useMemo(
    () =>
      turn.blocks.map((b) => ({
        ...b,
        activity: condenseActivityEntries(b.activity),
      })),
    [turn.blocks],
  );
  const [openPhases, setOpenPhases] = useState<Set<CopilotPhaseId>>(
    () => new Set(),
  );

  return (
    <div className="flex flex-col">
      {rows.map((row) => {
        const isActive = row.status === "active";
        const hasNest =
          row.id === "draft"
            ? row.entries.length > 0 ||
              (turn.draft?.blockLabels.length ?? 0) > 0 ||
              isActive
            : row.id === "test"
              ? row.entries.length > 0 || turn.blocks.length > 0
              : row.entries.length > 0;
        const open = isActive || openPhases.has(row.id);
        const toggleable = hasNest && !isActive;
        const rowContent = (
          <>
            <span
              className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-[11px] font-bold ${phasePuckClasses(
                row.status,
              )}`}
              aria-hidden="true"
            >
              {phaseGlyph(row.status)}
            </span>
            <span
              className={`flex-1 text-[12.5px] ${phaseLabelClasses(row.status)}`}
            >
              {row.label}
              <span className="sr-only"> · {phaseStatusWord(row.status)}</span>
            </span>
            {row.stub ? (
              <span
                className={`text-[11px] tabular-nums ${
                  row.status === "fail"
                    ? "text-rose-700 dark:text-rose-400"
                    : "text-muted-foreground dark:text-slate-500"
                }`}
              >
                {row.stub}
              </span>
            ) : null}
            {hasNest ? (
              <span
                className={`shrink-0 text-[12px] text-muted-foreground transition-transform dark:text-slate-500 ${
                  open ? "rotate-90" : ""
                }`}
                aria-hidden="true"
              >
                ›
              </span>
            ) : null}
          </>
        );
        const toggle = () =>
          setOpenPhases((prev) => {
            const next = new Set(prev);
            if (next.has(row.id)) {
              next.delete(row.id);
            } else {
              next.add(row.id);
            }
            return next;
          });

        return (
          <div key={row.id} className="flex flex-col">
            {toggleable ? (
              <button
                type="button"
                aria-expanded={open}
                onClick={toggle}
                className="flex w-full cursor-pointer items-center gap-3 px-1 py-1 text-left"
              >
                {rowContent}
              </button>
            ) : (
              // Active rows render inert (a button whose click is a no-op
              // would be a keyboard/screen-reader trap) — status is
              // conveyed via the sr-only word in rowContent instead.
              <div className="flex w-full items-center gap-3 px-1 py-1 text-left">
                {rowContent}
              </div>
            )}
            {open ? (
              <div className="ml-[25px] flex flex-col gap-1.5 rounded-lg border border-border/60 bg-slate-elevation1 px-3 py-2">
                {row.id === "draft" ? (
                  <>
                    {row.entries.map((entry) => (
                      <ActivityRow key={entry.id} entry={entry} />
                    ))}
                    {(turn.draft?.blockLabels ?? []).map((label) => (
                      <FSubRow
                        key={label}
                        glyph="✦"
                        glyphClass="text-emerald-700 dark:text-emerald-300"
                      >
                        <span className="text-muted-foreground">Drafted </span>
                        <span
                          className={
                            uxV1
                              ? "text-foreground"
                              : "font-mono text-foreground"
                          }
                          title={uxV1 ? label : undefined}
                        >
                          {uxV1 ? humanizeBlockLabel(label) : label}
                        </span>
                      </FSubRow>
                    ))}
                    {isActive ? <DraftPlaceholderNote turn={turn} /> : null}
                  </>
                ) : row.id === "test" ? (
                  <>
                    {row.entries.map((entry) => (
                      <ActivityRow key={entry.id} entry={entry} />
                    ))}
                    {condensedBlocks.map((b) => (
                      <FBlockRun
                        key={b.workflowRunBlockId || b.label}
                        block={b}
                        turnEnded={turnEnded}
                        onSelect={onBlockSelect}
                        uxV1={uxV1}
                        outcomeReasonFallback={collapsedOutcomeReason}
                      />
                    ))}
                  </>
                ) : (
                  row.entries.map((entry) => (
                    <ActivityRow key={entry.id} entry={entry} />
                  ))
                )}
              </div>
            ) : null}
          </div>
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
  onClick: () => void;
  subtitle?: ReactNode;
}

function TurnHead({ summary, expanded, onClick, subtitle }: TurnHeadProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-expanded={expanded}
      className="flex w-full items-start gap-3 px-3.5 py-3 text-left"
    >
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
      <span
        className={`mt-1 shrink-0 text-[14px] text-muted-foreground transition-transform dark:text-slate-500 ${
          expanded ? "rotate-90" : ""
        }`}
        aria-hidden="true"
      >
        ›
      </span>
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
  const closing =
    turn.narrativeSummary?.trim() || turn.terminalMessage?.trim() || "";
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
  const showCommit = !summary.isQA && completed.length > 0;
  const showChecklist = Boolean(uxV1) && showPhaseChecklist(turn);

  return (
    <div className="overflow-hidden rounded-xl border border-border/60 bg-slate-elevation2">
      <TurnHead
        summary={summary}
        expanded={false}
        onClick={onExpand}
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
          <FPhaseChecklist
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

      {failed.length > 0 ? (
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
    </div>
  );
}

interface DetailViewProps {
  turn: TurnNarrativeState;
  onCollapse: (() => void) | null;
  onBlockSelect?: (label: string) => void;
  uxV1?: boolean;
}

function DetailView({
  turn,
  onCollapse,
  onBlockSelect,
  uxV1,
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
          <FPhaseChecklist
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

      {!hasBlocks &&
      !designStarted &&
      !turn.terminal &&
      !["docs_answer", "refuse", "clarify"].includes(effectiveMode(turn)) ? (
        <div className="pl-9 text-[12px] italic text-muted-foreground dark:text-slate-500">
          Waiting for the first block to start…
        </div>
      ) : null}

      {turn.terminal && (turn.narrativeSummary || turn.terminalMessage) ? (
        <div className="whitespace-pre-wrap pl-9 pr-8 text-[13px] leading-[1.55] text-foreground dark:text-slate-200">
          {turn.narrativeSummary?.trim() || turn.terminalMessage?.trim()}
        </div>
      ) : null}
    </div>
  );
}

interface NarrativeViewProps {
  turn: TurnNarrativeState;
  onBlockSelect?: (blockLabel: string) => void;
  uxV1?: boolean;
}

export function NarrativeView({
  turn,
  onBlockSelect,
  uxV1,
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
    />
  );
}
