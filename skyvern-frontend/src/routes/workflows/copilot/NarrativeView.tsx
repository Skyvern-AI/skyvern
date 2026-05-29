import { BlockState, BlockUIState, TurnNarrativeState } from "./narrativeState";

const MODE_CHIP_UNKNOWN = {
  label: "THINKING",
  classes: "bg-slate-700/50 text-slate-200 border-slate-600",
};

const MODE_CHIP: Record<string, { label: string; classes: string }> = {
  build: {
    label: "BUILDING",
    classes: "bg-blue-900/40 text-blue-200 border-blue-700/60",
  },
  edit: {
    label: "EDITING",
    classes: "bg-indigo-900/40 text-indigo-200 border-indigo-700/60",
  },
  docs_answer: {
    label: "ANSWERING",
    classes: "bg-emerald-900/40 text-emerald-200 border-emerald-700/60",
  },
  clarify: {
    label: "CLARIFYING",
    classes: "bg-amber-900/40 text-amber-200 border-amber-700/60",
  },
  diagnose: {
    label: "DIAGNOSING",
    classes: "bg-orange-900/40 text-orange-200 border-orange-700/60",
  },
  draft_only: {
    label: "DRAFTING",
    classes: "bg-cyan-900/40 text-cyan-200 border-cyan-700/60",
  },
  refuse: {
    label: "REFUSING",
    classes: "bg-rose-900/40 text-rose-200 border-rose-700/60",
  },
  unknown: MODE_CHIP_UNKNOWN,
};

const BLOCK_STATE_CHIP: Record<
  BlockUIState,
  { label: string; classes: string }
> = {
  queued: {
    label: "queued",
    classes: "bg-slate-800 text-slate-400 border-slate-700",
  },
  running: {
    label: "running",
    classes: "bg-blue-900/40 text-blue-200 border-blue-700/60",
  },
  completed: {
    label: "done",
    classes: "bg-emerald-900/40 text-emerald-200 border-emerald-700/60",
  },
  failed: {
    label: "failed",
    classes: "bg-rose-900/40 text-rose-200 border-rose-700/60",
  },
  skipped: {
    label: "skipped",
    classes: "bg-slate-800 text-slate-500 border-slate-700",
  },
};

function ModeChip({ mode }: { mode: string }) {
  const cfg = MODE_CHIP[mode] ?? MODE_CHIP_UNKNOWN;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${cfg.classes}`}
    >
      {cfg.label}
    </span>
  );
}

function BlockCard({ block }: { block: BlockState }) {
  const stateChip = BLOCK_STATE_CHIP[block.state];
  return (
    <div className="flex items-center justify-between gap-2 rounded border border-slate-700/60 bg-slate-900/40 px-2 py-1 text-xs">
      <div className="flex min-w-0 flex-1 items-center gap-2">
        <span className="truncate font-mono text-slate-200">{block.label}</span>
        <span className="shrink-0 text-[10px] text-slate-500">
          {block.blockType}
        </span>
      </div>
      <span
        className={`shrink-0 rounded-full border px-1.5 py-px text-[9px] font-bold uppercase tracking-wide ${stateChip.classes}`}
      >
        {stateChip.label}
      </span>
    </div>
  );
}

export function NarrativeView({ turn }: { turn: TurnNarrativeState }) {
  const hasBlocks = turn.blocks.length > 0;
  const showDesignSpinner = turn.designStarted && !turn.designEnded;
  const showWaiting = turn.designEnded && !hasBlocks && !turn.terminal;

  return (
    <div className="flex flex-col gap-2 text-sm text-slate-200">
      <div className="flex items-center gap-2">
        <ModeChip mode={turn.mode} />
        {turn.draft ? (
          <span className="text-[11px] text-slate-400">
            Drafted workflow with {turn.draft.blockCount} block
            {turn.draft.blockCount === 1 ? "" : "s"}
            {turn.draft.blockLabels.length > 0 ? (
              <>
                :{" "}
                <span className="font-mono text-slate-300">
                  {turn.draft.blockLabels.join(", ")}
                </span>
              </>
            ) : null}
          </span>
        ) : null}
      </div>

      {showDesignSpinner ? (
        <div className="text-xs italic text-slate-400">
          Designing the workflow…
        </div>
      ) : null}

      {hasBlocks ? (
        <div className="flex flex-col gap-1">
          {turn.blocks.map((b) => (
            <BlockCard key={b.workflowRunBlockId} block={b} />
          ))}
        </div>
      ) : null}

      {showWaiting ? (
        <div className="text-xs italic text-slate-500">
          Waiting for the first block to start…
        </div>
      ) : null}

      {turn.terminal && turn.narrativeSummary ? (
        <div className="rounded border border-slate-700/40 bg-slate-900/30 px-2 py-1 text-xs text-slate-300">
          {turn.narrativeSummary}
        </div>
      ) : null}
    </div>
  );
}
