import { useState } from "react";
import {
  CheckCircledIcon,
  CrossCircledIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { Status, type ActionsApiResponse } from "@/api/types";
import { CopyButton } from "@/components/CopyButton";
import { Skeleton } from "@/components/ui/skeleton";
import { ActionCardCompact } from "@/routes/tasks/detail/ActionCardCompact";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { formatDuration, toDuration } from "@/routes/workflows/utils";
import { workflowBlockTitle } from "@/routes/workflows/editor/nodes/types";
import { WorkflowBlockIcon } from "@/routes/workflows/editor/nodes/WorkflowBlockIcon";
import { cn } from "@/util/utils";
import {
  isAction,
  type ObserverThought,
  type WorkflowRunBlock,
} from "../../types/workflowRunTypes";
import type { WorkflowRunOverviewActiveElement } from "../WorkflowRunOverview";
import { ThoughtCard } from "../ThoughtCard";
import { stringifyTimelineValue } from "./formatValue";

function TruncatedWithTooltip({
  full,
  className,
  side = "top",
}: {
  full: string;
  className?: string;
  side?: "top" | "bottom" | "left" | "right";
}) {
  return (
    <TooltipProvider delayDuration={300}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span className={cn("min-w-0 truncate", className)}>{full}</span>
        </TooltipTrigger>
        <TooltipContent
          side={side}
          className="max-w-md break-all font-mono text-[11px]"
        >
          {full}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

function StatusPill({ status }: { status: Status | null }) {
  if (status === null) return null;
  const isSuccess = status === Status.Completed;
  const isFailure =
    status === Status.Failed ||
    status === Status.Terminated ||
    status === Status.TimedOut ||
    status === Status.Canceled;
  const isRunning = status === Status.Running;

  if (isSuccess) {
    return (
      <span className="inline-flex items-center gap-1 rounded bg-success/15 px-2 py-0.5 text-xs text-success">
        <CheckCircledIcon className="size-3.5" />
        <span>Completed</span>
      </span>
    );
  }
  if (isFailure) {
    return (
      <span className="inline-flex items-center gap-1 rounded bg-destructive/15 px-2 py-0.5 text-xs text-destructive">
        <CrossCircledIcon className="size-3.5" />
        <span className="capitalize">{status}</span>
      </span>
    );
  }
  if (isRunning) {
    return (
      <span className="inline-flex items-center gap-1 rounded bg-sky-500/15 px-2 py-0.5 text-xs text-sky-300">
        <ReloadIcon className="size-3.5 animate-spin" />
        <span>Running</span>
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded bg-slate-700 px-2 py-0.5 text-xs capitalize text-slate-300">
      {status}
    </span>
  );
}

function BlockDetailHeader({
  block,
  iterationOverride,
}: {
  block: WorkflowRunBlock;
  iterationOverride?: number | null;
}) {
  const duration =
    block.duration !== null ? formatDuration(toDuration(block.duration)) : null;
  const hasIterationOverride =
    iterationOverride !== undefined && iterationOverride !== null;
  const hasResolvedIterationOverride =
    hasIterationOverride &&
    Array.isArray(block.loop_values) &&
    iterationOverride >= 0 &&
    iterationOverride < block.loop_values.length;
  const iterationIndex = hasResolvedIterationOverride
    ? iterationOverride
    : hasIterationOverride
      ? null
      : block.current_index;
  const iterationLabel =
    iterationIndex !== null && iterationIndex !== undefined
      ? `Iteration ${iterationIndex + 1}`
      : null;
  // When a loop block has an explicit iteration selection, source the chip
  // value from loop_values[i] so it matches the iteration label. Without
  // this, the chip displays block.current_value (the latest iteration)
  // alongside a label for an older iteration — a contradictory pairing.
  let valueToShow: unknown = null;
  if (hasResolvedIterationOverride && Array.isArray(block.loop_values)) {
    const resolvedIterationIndex = iterationOverride as number;
    valueToShow = block.loop_values[resolvedIterationIndex];
  } else if (!hasIterationOverride) {
    valueToShow = block.current_value;
  }
  const currentValueFull =
    valueToShow !== null && valueToShow !== undefined
      ? stringifyTimelineValue(valueToShow)
      : null;
  const currentValuePreview =
    currentValueFull !== null
      ? currentValueFull.replace(/\s+/g, " ").trim()
      : null;

  return (
    <div className="border-b border-slate-700 bg-slate-elevation1">
      <div
        data-slot="block-detail-header-primary"
        className="flex items-center gap-2 px-3 py-2"
      >
        <WorkflowBlockIcon
          workflowBlockType={block.block_type}
          className="size-4 shrink-0 text-slate-300"
        />
        <span className="min-w-0 truncate text-sm font-semibold text-slate-100">
          {workflowBlockTitle[block.block_type]}
        </span>
        <span className="ml-auto flex shrink-0 items-center gap-2">
          {duration && (
            <span className="text-[10px] tabular-nums text-slate-500">
              {duration}
            </span>
          )}
          <StatusPill status={block.status} />
        </span>
      </div>
      <div
        data-slot="block-detail-header-meta"
        className="flex min-w-0 items-center gap-1.5 px-3 pb-2 text-[11px] text-slate-500"
      >
        {block.label && (
          <>
            <TruncatedWithTooltip
              full={block.label}
              className="max-w-[12rem] text-slate-400"
            />
            <span className="shrink-0 text-slate-600">·</span>
          </>
        )}
        <TruncatedWithTooltip
          full={block.workflow_run_block_id}
          className="max-w-[11rem] font-mono text-[10px] text-slate-500"
        />
        {iterationLabel && (
          <span className="shrink-0 rounded bg-slate-800 px-1.5 py-0.5 text-[10px] font-medium text-slate-300">
            {iterationLabel}
          </span>
        )}
      </div>
      {currentValuePreview && (
        <div className="flex min-w-0 items-center gap-2 border-t border-slate-700/50 px-3 py-1.5 text-[11px] duration-200 animate-in fade-in slide-in-from-top-1">
          <span className="shrink-0 text-slate-500">Iterated value:</span>
          <TooltipProvider delayDuration={300}>
            <Tooltip>
              <TooltipTrigger asChild>
                <code className="min-w-0 truncate rounded bg-slate-elevation1 px-1.5 py-0.5 font-mono text-slate-300">
                  {currentValuePreview}
                </code>
              </TooltipTrigger>
              <TooltipContent
                side="bottom"
                className="max-w-md break-all font-mono text-[11px]"
              >
                {currentValueFull}
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
      )}
      {block.description && (
        <div className="border-t border-slate-700/50 px-3 py-2 duration-200 animate-in fade-in slide-in-from-top-1">
          <div className="text-xs text-slate-400">{block.description}</div>
        </div>
      )}
    </div>
  );
}

function BlockDetailHeaderSkeleton() {
  return (
    <div className="border-b border-slate-700 bg-slate-elevation1">
      <div className="flex items-center gap-2 px-3 py-2">
        <Skeleton className="size-4 shrink-0 rounded" />
        <Skeleton className="h-4 w-24 rounded" />
        <span className="ml-auto flex shrink-0 items-center gap-2">
          <Skeleton className="h-3 w-10 rounded" />
          <Skeleton className="h-5 w-20 rounded" />
        </span>
      </div>
      <div className="flex items-center gap-1.5 px-3 pb-2">
        <Skeleton className="h-3 w-16 rounded" />
        <Skeleton className="h-3 w-28 rounded" />
      </div>
    </div>
  );
}

function Section({
  title,
  children,
  className,
}: {
  title: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "space-y-1.5 duration-200 animate-in fade-in slide-in-from-top-2",
        className,
      )}
    >
      <div className="text-[11px] font-medium uppercase tracking-wide text-slate-500">
        {title}
      </div>
      <div className="text-sm text-slate-300">{children}</div>
    </div>
  );
}

function BlockDetailFailure({ block }: { block: WorkflowRunBlock }) {
  if (!block.failure_reason) return null;
  return (
    <div className="space-y-1.5 duration-200 animate-in fade-in slide-in-from-top-2">
      <div className="text-[11px] font-medium uppercase tracking-wide text-destructive">
        Failure
      </div>
      <div className="rounded border border-destructive/40 bg-destructive/10 px-2.5 py-2 text-xs leading-relaxed text-destructive">
        {block.failure_reason}
      </div>
    </div>
  );
}

function CodeBlock({
  children,
  className,
  copyValue,
}: {
  children: React.ReactNode;
  className?: string;
  copyValue?: string;
}) {
  // When copyValue is provided, render the block with a hover-revealed copy
  // affordance in the top-right corner. The wrapper has `group` so the
  // CopyButton uses opacity transitions to fade in.
  if (copyValue !== undefined) {
    return (
      <div className={cn("group relative", className)}>
        <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded bg-slate-elevation1 p-2.5 pr-10 font-mono text-xs text-slate-200">
          {children}
        </pre>
        <div className="absolute right-1 top-1 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
          <CopyButton value={copyValue} />
        </div>
      </div>
    );
  }
  return (
    <pre
      className={cn(
        "overflow-x-auto whitespace-pre-wrap break-all rounded bg-slate-elevation1 p-2.5 font-mono text-xs text-slate-200",
        className,
      )}
    >
      {children}
    </pre>
  );
}

function JsonView({ value }: { value: unknown }) {
  const text = stringifyTimelineValue(value);
  return <CodeBlock copyValue={text}>{text}</CodeBlock>;
}

function GoalText({ text }: { text: string | null | undefined }) {
  if (!text) return null;
  return (
    <div className="whitespace-pre-wrap text-sm leading-relaxed text-slate-300">
      {text}
    </div>
  );
}

function BlockActionList({
  block,
  activeItem,
  onActionSelect,
}: {
  block: WorkflowRunBlock;
  activeItem: WorkflowRunOverviewActiveElement;
  onActionSelect?: (payload: {
    block: WorkflowRunBlock;
    action: ActionsApiResponse;
  }) => void;
}) {
  const [expandedActionId, setExpandedActionId] = useState<string | null>(null);
  const actions = block.actions ?? [];
  const actionsTopDown = [...actions].reverse();
  if (actions.length === 0) return null;
  return (
    <Section title={`Actions (${actions.length})`}>
      <div className="space-y-2">
        {actionsTopDown.map((action, index) => {
          const isActive =
            isAction(activeItem) && activeItem.action_id === action.action_id;
          return (
            <ActionCardCompact
              key={action.action_id}
              action={action}
              active={isActive}
              index={index + 1}
              expanded={expandedActionId === action.action_id}
              onToggleExpanded={() => {
                setExpandedActionId((prev) =>
                  prev === action.action_id ? null : action.action_id,
                );
              }}
              onSelect={() => {
                onActionSelect?.({ block, action });
              }}
            />
          );
        })}
      </div>
    </Section>
  );
}

function BlockThoughtList({
  thoughts,
  activeItem,
  onSelect,
}: {
  thoughts: Array<ObserverThought>;
  activeItem: WorkflowRunOverviewActiveElement;
  onSelect?: (thought: ObserverThought) => void;
}) {
  if (thoughts.length === 0) return null;
  return (
    <Section title={`Thoughts (${thoughts.length})`}>
      <div className="space-y-2">
        {thoughts.map((thought) => {
          const isActive =
            activeItem !== null &&
            activeItem !== "stream" &&
            typeof activeItem === "object" &&
            "thought_id" in activeItem &&
            activeItem.thought_id === thought.thought_id;
          return (
            <ThoughtCard
              key={thought.thought_id}
              active={isActive}
              thought={thought}
              onClick={() => onSelect?.(thought)}
            />
          );
        })}
      </div>
    </Section>
  );
}

export {
  BlockActionList,
  BlockDetailFailure,
  BlockDetailHeader,
  BlockDetailHeaderSkeleton,
  BlockThoughtList,
  CodeBlock,
  GoalText,
  JsonView,
  Section,
  StatusPill,
};
