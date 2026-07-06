import { cn } from "@/util/utils";

import type { DebugSessionRun } from "../../hooks/useDebugSessionRunsQuery";
import type { WorkflowBlockType } from "../../types/workflowTypes";
import type { RecentActivityViewProps } from "./viewProps";
import {
  getBlockTypeTitle,
  getRunAbsoluteTime,
  getRunActivityKey,
  getRunAgoLabel,
  getRunDurationLabel,
  getRunModeLabel,
  getRunStatusKind,
  getRunStatusLabel,
  normalizeReason,
  STATUS_PILL_TONE,
} from "./runActivity";
import { RunBlockGlyph, RunStatusGlyph } from "./RunGlyphs";

function RecentActivityRow({
  run,
  isCurrent,
  isWorkflowRunning,
  blockType,
  now,
  onSelect,
}: {
  run: DebugSessionRun;
  isCurrent: boolean;
  isWorkflowRunning: boolean;
  blockType: WorkflowBlockType | undefined;
  now: number;
  onSelect: (run: DebugSessionRun) => void;
}) {
  const kind = getRunStatusKind(run.status, isWorkflowRunning);
  const duration = getRunDurationLabel(run);
  const ago = getRunAgoLabel(run, now);
  const absoluteTime = getRunAbsoluteTime(run);
  const failureReason =
    kind === "failure" ? normalizeReason(run.failure_reason) : null;

  return (
    <li>
      <button
        type="button"
        onClick={isWorkflowRunning ? undefined : () => onSelect(run)}
        aria-current={isCurrent ? "true" : undefined}
        aria-disabled={isWorkflowRunning || undefined}
        title={
          isWorkflowRunning
            ? "Can't switch runs while this run is in progress"
            : undefined
        }
        className={cn(
          "flex w-full flex-col gap-1 rounded-md px-2 py-1.5 text-left outline-none transition-colors",
          isWorkflowRunning
            ? "cursor-not-allowed opacity-50"
            : "hover:bg-white/5 focus-visible:ring-1 focus-visible:ring-white/40",
          isCurrent && "bg-white/10",
        )}
      >
        <div className="flex items-center gap-2">
          <RunStatusGlyph
            status={run.status}
            isWorkflowRunning={isWorkflowRunning}
            className="size-3.5"
          />
          <RunBlockGlyph
            blockType={blockType}
            className="size-3.5 text-slate-300"
          />
          <span className="min-w-0 flex-1 truncate text-xs font-medium text-slate-100">
            {run.block_label}
          </span>
          {ago && (
            <span
              title={absoluteTime ?? undefined}
              className="shrink-0 text-[10px] tabular-nums text-slate-500"
            >
              {ago}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5 pl-[1.625rem]">
          <span className="rounded bg-slate-700/70 px-1.5 py-0.5 text-[10px] font-medium text-slate-300">
            {getBlockTypeTitle(blockType)}
          </span>
          <span className="rounded border border-white/10 px-1.5 py-0.5 text-[10px] font-medium text-slate-400">
            {getRunModeLabel(run)}
          </span>
          <span
            className={cn(
              "rounded px-1.5 py-0.5 text-[10px] font-medium",
              STATUS_PILL_TONE[kind],
            )}
          >
            {getRunStatusLabel(run.status)}
          </span>
          {duration && (
            <span className="ml-auto shrink-0 text-[10px] tabular-nums text-slate-500">
              {duration}
            </span>
          )}
        </div>
        {failureReason && (
          <div className="line-clamp-2 pl-[1.625rem] text-[10px] leading-snug text-destructive/80">
            {failureReason}
          </div>
        )}
      </button>
    </li>
  );
}

function RecentActivityList({
  runs,
  currentActivityKey,
  isWorkflowRunning,
  blockTypeByLabel,
  now,
  onSelect,
}: RecentActivityViewProps) {
  // Newest first in the list, the natural reading order for "recent activity".
  const ordered = [...runs].reverse();

  return (
    <div className="flex w-[26rem] max-w-[80vw] flex-col overflow-hidden rounded-lg border border-white/10 bg-[#0b0b0b] text-slate-200 shadow-xl">
      <div className="flex items-center justify-between border-b border-white/10 px-3 py-2">
        <span className="text-xs font-medium text-slate-300">
          Recent activity
        </span>
        <span className="text-[10px] tabular-nums text-slate-500">
          {runs.length} {runs.length === 1 ? "run" : "runs"}
        </span>
      </div>
      <ul className="flex max-h-[20rem] flex-col gap-0.5 overflow-y-auto p-1">
        {ordered.map((run) => {
          const activityKey = getRunActivityKey(run);
          return (
            <RecentActivityRow
              key={activityKey}
              run={run}
              isCurrent={currentActivityKey === activityKey}
              isWorkflowRunning={isWorkflowRunning}
              blockType={blockTypeByLabel.get(run.block_label)}
              now={now}
              onSelect={onSelect}
            />
          );
        })}
      </ul>
    </div>
  );
}

export { RecentActivityList };
