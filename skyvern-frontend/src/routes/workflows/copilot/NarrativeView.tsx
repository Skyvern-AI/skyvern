import { type ReactNode, useEffect, useMemo, useState } from "react";

import { buildRevealOffsets, revealedCountAt } from "./actionReveal";
import {
  ActivityEntry,
  BlockState,
  RecordedActionSummary,
  TurnNarrativeState,
  TurnSummary,
  computeTurnSummary,
  effectiveMode,
  formatElapsed,
  isBlockOk,
  latestBlocksByLabel,
  parseUtcIsoMs,
  toolActivityDisplayLabel,
} from "./narrativeState";
import { useShimmerText } from "../workflowRun/useShimmerText";

// Row flashes green/red for 600ms once revealed — must match the tailwind
// copilot-row-flash-* animation duration.
const FLASH_WINDOW_MS = 600;

interface BlockPalette {
  fg: string;
  bg: string;
  border: string;
  glyph: string;
}

const PALETTE_NAV: BlockPalette = {
  fg: "text-blue-300",
  bg: "bg-blue-500/15",
  border: "border-blue-400/60",
  glyph: "→",
};
const PALETTE_CRED: BlockPalette = {
  fg: "text-amber-300",
  bg: "bg-amber-500/15",
  border: "border-amber-400/60",
  glyph: "⌬",
};
const PALETTE_LOOP: BlockPalette = {
  fg: "text-sky-300",
  bg: "bg-sky-500/15",
  border: "border-sky-400/60",
  glyph: "↻",
};
const PALETTE_ACTION: BlockPalette = {
  fg: "text-emerald-300",
  bg: "bg-emerald-500/15",
  border: "border-emerald-400/60",
  glyph: "✦",
};
const PALETTE_EXTRACTION: BlockPalette = {
  fg: "text-sky-300",
  bg: "bg-sky-500/15",
  border: "border-sky-400/60",
  glyph: "↓",
};
const PALETTE_TASK: BlockPalette = {
  fg: "text-slate-300",
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
        muted ? "text-slate-400" : "text-slate-200",
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
        className={`mt-[2px] flex w-3.5 shrink-0 justify-center text-[11px] font-bold ${glyphClass ?? "text-slate-400"}`}
        aria-hidden="true"
      >
        {glyph}
      </span>
      <div
        className={[
          "min-w-0 flex-1 text-[11.5px] leading-[1.55]",
          muted ? "text-slate-400" : "text-slate-200",
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

function ActivityRow({ entry }: { entry: ActivityEntry }) {
  if (entry.kind === "narration") {
    return (
      <FSubRow glyph="✦" glyphClass="text-sky-300" italic muted>
        {entry.text}
      </FSubRow>
    );
  }
  if (entry.kind === "tool_call") {
    const label =
      entry.displayLabel ?? toolActivityDisplayLabel(entry.toolName);
    return (
      <FSubRow glyph="▸" glyphClass="text-slate-400">
        <span className="text-slate-200">{label}</span>
        <span className="text-slate-500"> · calling…</span>
      </FSubRow>
    );
  }
  const ok = entry.success !== false;
  return (
    <FSubRow
      glyph={ok ? "✓" : "✕"}
      glyphClass={ok ? "text-emerald-300" : "text-rose-300"}
    >
      <span className={ok ? "text-slate-200" : "text-rose-200"}>
        {entry.text}
      </span>
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
      <FSubRow glyph={<Spinner small />} glyphClass="text-blue-300">
        <span ref={shimmerRef} className="text-slate-200">
          {action.label}
        </span>
        {action.summary ? (
          <span className="text-slate-500"> · {action.summary}</span>
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
      glyphClass={action.failed ? "text-rose-300" : "text-emerald-300"}
    >
      <span
        className={`${action.failed ? "text-rose-200" : "text-slate-200"} ${flashClass}`}
      >
        {action.label}
      </span>
      {action.summary ? (
        <span className="text-slate-500"> · {action.summary}</span>
      ) : null}
    </FSubRow>
  );
}

interface FBlockRunProps {
  block: BlockState;
  turnEnded: boolean;
  onSelect?: (label: string) => void;
}

function FBlockRun({ block, turnEnded, onSelect }: FBlockRunProps) {
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
    ? "text-blue-300"
    : isOk
      ? "text-emerald-300"
      : isOutcomeNotShown
        ? "text-amber-300"
        : isFail
          ? "text-rose-300"
          : isVerifying || isRanNeutral
            ? "text-slate-300"
            : "text-slate-400";
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
            <span className="font-mono text-[12.5px] font-semibold text-slate-100">
              {block.label}
            </span>
            <span className="text-[11px] text-slate-500">·</span>
            <span className={`font-mono text-[11px] font-medium ${accentText}`}>
              {statusText}
            </span>
            <span className="text-[10.5px] text-slate-500">
              · {block.blockType}
            </span>
          </div>
          {!open && isOk && block.activity.length > 0 ? (
            <div className="mt-0.5 text-[12px] leading-[1.5] text-slate-400">
              {block.activity[block.activity.length - 1]!.text}
            </div>
          ) : null}
          {!open && isOutcomeNotShown ? (
            <div className="mt-0.5 text-[12px] leading-[1.5] text-amber-200/80">
              Outcome not confirmed — the run finished without showing the goal
              was met.
            </div>
          ) : null}
        </div>
        {toggleable ? (
          <span
            className={`shrink-0 text-[12px] text-slate-500 transition-transform ${
              open ? "rotate-90" : ""
            }`}
            aria-hidden="true"
          >
            ›
          </span>
        ) : null}
      </button>

      {open ? (
        <div className="ml-9 flex flex-col gap-1.5 border-l border-slate-700/60 py-1.5 pl-3">
          {isRunning ? (
            <span className="inline-flex w-fit items-center gap-1.5 rounded-full border border-blue-400/40 bg-blue-500/10 px-2 py-0.5 text-[11px] font-semibold text-blue-300">
              <span className="h-[5px] w-[5px] animate-pulse rounded-full bg-blue-400" />
              Active in Live Browser
            </span>
          ) : null}
          {block.activity.length === 0 && isRunning ? (
            <FSubRow glyph={<Spinner small />} glyphClass="text-blue-300">
              <span className="text-slate-400">Working…</span>
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
              <span className="text-[11px] font-bold text-rose-300">✕</span>
              <div className="text-[12px] leading-[1.5] text-rose-200/90">
                {block.activity.find((e) => e.kind === "tool_result")?.text ??
                  "Halted — see run details."}
              </div>
            </div>
          ) : null}
          {isOutcomeNotShown ? (
            <div className="mt-1 flex items-start gap-2 rounded-md border border-amber-400/30 bg-amber-500/10 px-2.5 py-1.5">
              <span className="text-[11px] font-bold text-amber-300">!</span>
              <div className="text-[12px] leading-[1.5] text-amber-200/90">
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
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-sky-400/60 bg-sky-500/15 text-[11px] font-bold text-sky-300"
          aria-hidden="true"
        >
          {done ? "✓" : <Spinner />}
        </span>
        <div className="flex flex-1 items-baseline gap-2 text-left">
          <span className="text-[12.5px] font-semibold text-slate-100">
            {title}
          </span>
          {summary.length ? (
            <span className="text-[11px] text-slate-400">
              · {summary.join(" · ")}
            </span>
          ) : null}
          {!done ? (
            <span className="text-[10.5px] uppercase tracking-wide text-blue-300">
              live
            </span>
          ) : null}
        </div>
        <span
          className={`shrink-0 text-[12px] text-slate-500 transition-transform ${
            open ? "rotate-90" : ""
          }`}
          aria-hidden="true"
        >
          ›
        </span>
      </button>
      {open ? (
        <div className="ml-9 flex flex-col gap-1 border-l border-slate-700/60 py-1.5 pl-3">
          {activity.map((entry) => (
            <ActivityRow key={entry.id} entry={entry} />
          ))}
          {blockLabels.map((label) => (
            <FSubRow key={label} glyph="✦" glyphClass="text-emerald-300">
              <span className="text-slate-400">Drafted </span>
              <span className="font-mono text-slate-100">{label}</span>
            </FSubRow>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function accentBg(accent: TurnSummary["accent"]): string {
  if (accent === "fail") {
    return "border-rose-400/60 bg-rose-500/15 text-rose-300";
  }
  if (accent === "qa") {
    return "border-sky-400/60 bg-sky-500/15 text-sky-300";
  }
  return "border-emerald-400/60 bg-emerald-500/15 text-emerald-300";
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
          <span className="text-[14px] font-semibold tracking-tight text-slate-100">
            {summary.headline}
          </span>
          {summary.stats.length ? (
            <span className="text-[11.5px] text-slate-400">
              {summary.stats.join(" · ")}
            </span>
          ) : null}
        </div>
        {subtitle}
      </div>
      <span
        className={`mt-1 shrink-0 text-[14px] text-slate-500 transition-transform ${
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
}

function RollupCard({ turn, summary, onExpand }: RollupCardProps) {
  const closing =
    turn.narrativeSummary?.trim() || turn.terminalMessage?.trim() || "";
  const rollupBlocks = latestBlocksByLabel(turn.blocks);
  const completed = rollupBlocks.filter((b) => isBlockOk(b));
  const failed = rollupBlocks.filter((b) => b.state === "failed");
  const showCommit = !summary.isQA && completed.length > 0;

  return (
    <div className="overflow-hidden rounded-xl border border-slate-700/60 bg-slate-elevation2">
      <TurnHead
        summary={summary}
        expanded={false}
        onClick={onExpand}
        subtitle={
          closing ? (
            <div
              className={`mt-0.5 text-[12.5px] leading-[1.5] ${
                summary.isFail && !summary.isStoppedWithDraft
                  ? "text-rose-200/90"
                  : "text-slate-400"
              }`}
            >
              {closing}
            </div>
          ) : null
        }
      />

      {showCommit ? (
        <div className="border-t border-white/5 pb-3 pl-[52px] pr-3.5 pt-2.5">
          <div className="mb-1.5 text-[10px] font-bold uppercase tracking-[.06em] text-slate-500">
            What changed
          </div>
          <ul className="m-0 flex list-none flex-col gap-1 p-0">
            {completed.map((b) => {
              const palette = paletteFor(b.blockType);
              return (
                <li
                  key={b.label}
                  className="flex items-baseline gap-1.5 text-[12px] leading-[1.5] text-slate-200"
                >
                  <span
                    className={`w-3.5 shrink-0 text-center text-[11px] font-bold ${palette.fg}`}
                    aria-hidden="true"
                  >
                    {palette.glyph}
                  </span>
                  <span className="font-mono text-[11px] text-slate-400">
                    {b.label}
                  </span>
                  <span className="text-slate-600">·</span>
                  <span className="text-[11.5px] text-slate-200">
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
          <div className="mb-1.5 text-[10px] font-bold uppercase tracking-[.06em] text-rose-400">
            Halted
          </div>
          <ul className="m-0 flex list-none flex-col gap-1 p-0">
            {failed.map((b) => (
              <li
                key={b.label}
                className="flex items-baseline gap-1.5 text-[12px] leading-[1.5] text-rose-200"
              >
                <span
                  className="w-3.5 shrink-0 text-center text-[11px] font-bold text-rose-300"
                  aria-hidden="true"
                >
                  ✕
                </span>
                <span className="font-mono text-[11px] text-rose-300/80">
                  {b.label}
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
}

function DetailView({ turn, onCollapse, onBlockSelect }: DetailViewProps) {
  const hasBlocks = turn.blocks.length > 0;
  const designStarted = turn.designStarted;
  const designOpen = designStarted && !turn.designEnded;
  // Hide the "Designed the workflow" cluster on terminal turns that produced
  // no draft (Q&A / clarify / refuse routes occasionally emit design_start
  // before the agent decides not to build). Live turns still surface it so a
  // long design phase isn't silently invisible.
  const hasDraft = (turn.draft?.blockCount ?? 0) > 0;
  const showDesign = designStarted && (hasDraft || hasBlocks || !turn.terminal);
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
          className="flex w-full items-center justify-end gap-1.5 px-3.5 py-1.5 text-[11px] font-medium uppercase tracking-wide text-slate-500 hover:text-slate-300"
        >
          <span>Collapse</span>
          <span aria-hidden="true" className="rotate-90 text-[13px]">
            ›
          </span>
        </button>
      ) : null}

      {showDesign ? (
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

      {hasBlocks ? (
        <div className="flex flex-col gap-1">
          {turn.blocks.map((b) => (
            <FBlockRun
              key={b.workflowRunBlockId || b.label}
              block={b}
              turnEnded={turn.terminal !== null}
              onSelect={onBlockSelect}
            />
          ))}
        </div>
      ) : null}

      {!hasBlocks &&
      !designStarted &&
      !turn.terminal &&
      !["docs_answer", "refuse", "clarify"].includes(effectiveMode(turn)) ? (
        <div className="pl-9 text-[12px] italic text-slate-500">
          Waiting for the first block to start…
        </div>
      ) : null}

      {turn.terminal && (turn.narrativeSummary || turn.terminalMessage) ? (
        <div className="whitespace-pre-wrap pl-9 pr-8 text-[13px] leading-[1.55] text-slate-200">
          {turn.narrativeSummary?.trim() || turn.terminalMessage?.trim()}
        </div>
      ) : null}
    </div>
  );
}

interface NarrativeViewProps {
  turn: TurnNarrativeState;
  onBlockSelect?: (blockLabel: string) => void;
}

export function NarrativeView({ turn, onBlockSelect }: NarrativeViewProps) {
  const summary = useMemo(() => computeTurnSummary(turn), [turn]);
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
      />
    );
  }

  return (
    <DetailView
      turn={turn}
      onCollapse={isComplete ? () => setUserRolled(true) : null}
      onBlockSelect={onBlockSelect}
    />
  );
}
