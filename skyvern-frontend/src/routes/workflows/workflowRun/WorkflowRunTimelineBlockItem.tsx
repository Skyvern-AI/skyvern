import {
  CheckCircledIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  CrossCircledIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import {
  type ActionsApiResponse,
  ActionTypes,
  getReadableActionType,
  Status,
} from "@/api/types";
import { Collapsible, CollapsibleContent } from "@/components/ui/collapsible";
import { formatDuration, toDuration } from "@/routes/workflows/utils";
import { cn } from "@/util/utils";
import {
  CODE_BLOCK_FALLBACK_TITLE,
  getCodeBlockTitle,
  workflowBlockTitle,
} from "../editor/nodes/types";
import { WorkflowBlockIcon } from "../editor/nodes/WorkflowBlockIcon";
import { actionTypeIcons as timelineActionIcons } from "../components/actionTypeIcons";
import { getActionDisplayStatus } from "../components/actionStatus";
import { TerminatedIcon, terminatedTone } from "@/components/terminatedVisual";
import {
  isAction,
  isBlockItem,
  isObserverThought,
  isThoughtItem,
  isWorkflowRunBlock,
  ObserverThought,
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";
import { type CodeBlockStep, WorkflowBlockTypes } from "../types/workflowTypes";
import { findCodeStepForLine } from "../workflowBlockUtils";
import {
  ActionItem,
  WorkflowRunOverviewActiveElement,
} from "./WorkflowRunOverview";
import { ThoughtCard } from "./ThoughtCard";
import {
  aggregateIterationStatus,
  type SkippedBranchMetadata,
  type UnexecutedDefinedBlock,
} from "./workflowTimelineUtils";
import { WorkflowRunTimelineUnexecutedBlockItem } from "./WorkflowRunTimelineUnexecutedBlockItem";

type SkippedBranchGroup = {
  key: string;
  branch: SkippedBranchMetadata;
  blocks: Array<UnexecutedDefinedBlock>;
};

type Props = {
  activeItem: WorkflowRunOverviewActiveElement;
  activeIteration?: number | null;
  block: WorkflowRunBlock;
  subItems: Array<WorkflowRunTimelineItem>;
  depth?: number;
  blockOrder?: ReadonlyMap<string, number>;
  codeStepsByLabel?: ReadonlyMap<string, Array<CodeBlockStep>>;
  skippedBranchBlocksByConditionalId?: ReadonlyMap<
    string,
    Array<SkippedBranchGroup>
  >;
  onBlockItemClick: (block: WorkflowRunBlock) => void;
  onIterationClick?: (
    loopBlock: WorkflowRunBlock,
    iterationIndex: number,
  ) => void;
  onActionClick: (action: ActionItem) => void;
  onThoughtClick?: (thought: ObserverThought) => void;
  renderThoughts?: boolean;
  finallyBlockLabel?: string | null;
  workflowRunIsFinalized?: boolean;
};

type LoopIterationGroup = {
  index: number | null;
  currentValue: string | null;
  items: Array<WorkflowRunTimelineItem>;
};

const INDENT_PX = 14;
const MAX_INDENT_RAIL_DEPTH = 6;
const RAIL_HIGHLIGHT_OFFSET_PX = INDENT_PX / 2;
const RAIL_CONTENT_PADDING_PX = INDENT_PX - 1;

const railHighlightStyle = {
  marginLeft: `-${RAIL_HIGHLIGHT_OFFSET_PX}px`,
  paddingLeft: `${RAIL_CONTENT_PADDING_PX}px`,
};

function IndentRails({ depth }: { depth: number }) {
  // Render guide rails only for nested rows. Top-level rows should start with
  // content, not a phantom outer timeline rail.
  // Cap rails so deeply nested loops/conditionals do not squeeze row content.
  const rails = Math.min(depth, MAX_INDENT_RAIL_DEPTH);
  return (
    <>
      {Array.from({ length: rails }).map((_, i) => (
        <div
          key={i}
          className="relative shrink-0 self-stretch"
          style={{ width: `${INDENT_PX}px` }}
        >
          <div className="absolute inset-y-0 left-1/2 w-px bg-border" />
        </div>
      ))}
    </>
  );
}

function StatusDot({
  status,
  isFinalized,
}: {
  status: Status | null;
  isFinalized: boolean;
}) {
  const isCompleted = status === Status.Completed;
  const isTerminated = status === Status.Terminated;
  const isFailure =
    status === Status.Failed ||
    status === Status.TimedOut ||
    status === Status.Canceled;
  const isRunning = status === Status.Running && !isFinalized;

  if (isCompleted) {
    return <CheckCircledIcon className="size-3.5 shrink-0 text-success" />;
  }
  if (isTerminated) {
    return <TerminatedIcon className={`size-3.5 shrink-0 ${terminatedTone}`} />;
  }
  if (isFailure) {
    return <CrossCircledIcon className="size-3.5 shrink-0 text-destructive" />;
  }
  if (isRunning) {
    return (
      <ReloadIcon className="size-3.5 shrink-0 animate-spin text-sky-700 dark:text-sky-400" />
    );
  }
  return (
    <div className="size-2 shrink-0 rounded-full bg-muted-foreground dark:bg-slate-600" />
  );
}

function normalizeInlineText(value: string | null | undefined): string | null {
  const normalized = value?.replace(/\s+/g, " ").trim();
  return normalized ? normalized : null;
}

function getActionSummary(action: ActionsApiResponse): string | null {
  return (
    normalizeInlineText(action.reasoning) ??
    normalizeInlineText(action.text) ??
    normalizeInlineText(action.response)
  );
}

function getRecordedActionMeta(action: ActionsApiResponse): {
  codeLine: number | null;
  durationMs: number | null;
} {
  const output = action.output;
  if (!output || typeof output !== "object" || Array.isArray(output)) {
    return { codeLine: null, durationMs: null };
  }
  const record = output as Record<string, unknown>;
  return {
    codeLine: typeof record.code_line === "number" ? record.code_line : null,
    durationMs:
      typeof record.duration_ms === "number" ? record.duration_ms : null,
  };
}

function formatActionDurationMs(durationMs: number): string {
  const seconds = durationMs / 1000;
  if (seconds >= 60) {
    return formatDuration(toDuration(seconds));
  }
  return `${seconds.toFixed(1).replace(/\.0$/, "")}s`;
}

type ActionRowPresentation = {
  icon: ReactNode;
  label: string;
  summary: string | null;
  tone: "default" | "error";
};

// Rows for code blocks summarize recorded actions as
// "<plain-English step> · line N · <duration>s"; the synthetic failure row
// (failed null_action) is labeled Error instead of Screenshot. The leading
// text reuses the matched definition step's plain-English copy so a fired
// action reads the same as the editor, falling back to the action's own
// reasoning and finally to the readable action type chip.
function getCodeActionRowPresentation(
  action: ActionsApiResponse,
  matchedStep: CodeBlockStep | null,
): ActionRowPresentation {
  const isCodeError =
    action.status === Status.Failed &&
    action.action_type === ActionTypes.NullAction;
  const label = isCodeError
    ? "Error"
    : getReadableActionType(action.action_type, { nullActionLabel: "Step" });
  const icon = isCodeError ? (
    <CrossCircledIcon className="size-3.5" />
  ) : action.action_type === ActionTypes.NullAction ? (
    <WorkflowBlockIcon workflowBlockType="code" className="size-3.5" />
  ) : (
    timelineActionIcons[action.action_type]
  );
  const { codeLine, durationMs } = getRecordedActionMeta(action);
  const stepText =
    !isCodeError && matchedStep
      ? (normalizeInlineText(matchedStep.title) ??
        normalizeInlineText(matchedStep.description))
      : null;
  const parts = [
    stepText ??
      getActionSummary(action) ??
      normalizeInlineText(action.description),
    codeLine !== null ? `line ${codeLine}` : null,
    durationMs !== null ? formatActionDurationMs(durationMs) : null,
  ].filter((part): part is string => part !== null);
  return {
    icon,
    label,
    summary: parts.length > 0 ? parts.join(" · ") : null,
    tone: isCodeError ? "error" : "default",
  };
}

function countSchemaFields(value: WorkflowRunBlock["data_schema"]): number {
  if (!value || typeof value !== "object" || Array.isArray(value)) return 0;
  const properties = "properties" in value ? value.properties : null;
  if (
    properties &&
    typeof properties === "object" &&
    !Array.isArray(properties)
  ) {
    return Object.keys(properties).length;
  }
  return Object.keys(value).length;
}

function getTimelineDescriptor(block: WorkflowRunBlock): string {
  const explicit =
    normalizeInlineText(block.description) ??
    normalizeInlineText(block.navigation_goal) ??
    normalizeInlineText(block.data_extraction_goal) ??
    normalizeInlineText(block.prompt) ??
    normalizeInlineText(block.instructions) ??
    normalizeInlineText(block.url);

  if (explicit) return explicit;

  if (block.block_type === "extraction") {
    const fieldCount = countSchemaFields(block.data_schema);
    if (fieldCount > 0) {
      return `Extract ${fieldCount} ${fieldCount === 1 ? "field" : "fields"}`;
    }
  }

  if (block.block_type === "for_loop") {
    const valueCount = Array.isArray(block.loop_values)
      ? block.loop_values.length
      : 0;
    return valueCount > 0
      ? `Loop over ${valueCount} ${valueCount === 1 ? "value" : "values"}`
      : "Loop over values";
  }

  if (block.block_type === "while_loop") {
    return "Repeat while condition passes";
  }

  if (block.block_type === "conditional") {
    const expression = normalizeInlineText(block.executed_branch_expression);
    return expression ? `Branch on ${expression}` : "Branch on a condition";
  }

  return `${workflowBlockTitle[block.block_type]} block`;
}

function getTimelineTypeLabel(block: WorkflowRunBlock): string {
  switch (block.block_type) {
    case "conditional":
      return "Condition";
    case "for_loop":
    case "while_loop":
      return "Loop";
    case "navigation":
    case "task":
    case "task_v2":
      return "Task";
    case "http_request":
      return "HTTP";
    default:
      return workflowBlockTitle[block.block_type];
  }
}

// getCodeBlockTitle ends at the bare "Code" label for prompt-less runs, which
// dropped the reasoning subtitle the timeline used to show. Fall back to the
// block reasoning (description) before bare "Code", normalized like a prompt.
function getCodeBlockTimelineName(
  block: WorkflowRunBlock,
  steps: Array<CodeBlockStep>,
): string {
  const title = getCodeBlockTitle({ prompt: block.prompt, steps });
  if (title !== CODE_BLOCK_FALLBACK_TITLE) {
    return title;
  }
  const reasoning = normalizeInlineText(block.description);
  return reasoning
    ? getCodeBlockTitle({ prompt: reasoning, steps: [] })
    : title;
}

function getLoopIterationGroups(
  items: Array<WorkflowRunTimelineItem>,
): Array<LoopIterationGroup> {
  const groupsByKey = new Map<string, LoopIterationGroup>();
  const unknownItems: Array<WorkflowRunTimelineItem> = [];

  items.forEach((item) => {
    const currentIndex = isBlockItem(item) ? item.block.current_index : null;
    const currentValue = isBlockItem(item) ? item.block.current_value : null;

    if (currentIndex === null) {
      unknownItems.push(item);
      return;
    }

    const groupKey = `index-${currentIndex}`;
    if (!groupsByKey.has(groupKey)) {
      groupsByKey.set(groupKey, {
        index: currentIndex,
        currentValue,
        items: [],
      });
    }
    const group = groupsByKey.get(groupKey)!;
    if (!group.currentValue && currentValue) {
      group.currentValue = currentValue;
    }
    group.items.push(item);
  });

  if (unknownItems.length > 0) {
    if (groupsByKey.size > 0) {
      let maxIndex = -1;
      let maxGroup: LoopIterationGroup | null = null;
      for (const group of groupsByKey.values()) {
        if (group.index !== null && group.index > maxIndex) {
          maxIndex = group.index;
          maxGroup = group;
        }
      }
      if (maxGroup) {
        unknownItems.forEach((item) => maxGroup!.items.push(item));
      }
    } else {
      groupsByKey.set("index-0", {
        index: 0,
        currentValue: null,
        items: unknownItems,
      });
    }
  }

  return Array.from(groupsByKey.values()).sort((left, right) => {
    if (left.index === null && right.index === null) return 0;
    if (left.index === null) return -1;
    if (right.index === null) return 1;
    return left.index - right.index;
  });
}

function timelineItemsContainActiveElement(
  items: Array<WorkflowRunTimelineItem>,
  activeItem: WorkflowRunOverviewActiveElement,
): boolean {
  if (activeItem === null || activeItem === "stream") return false;

  const stack = [...items];
  while (stack.length > 0) {
    const item = stack.pop()!;

    if (
      isBlockItem(item) &&
      isWorkflowRunBlock(activeItem) &&
      item.block.workflow_run_block_id === activeItem.workflow_run_block_id
    ) {
      return true;
    }
    if (
      isBlockItem(item) &&
      isAction(activeItem) &&
      item.block.actions?.some((a) => a.action_id === activeItem.action_id)
    ) {
      return true;
    }
    if (
      isThoughtItem(item) &&
      isObserverThought(activeItem) &&
      item.thought.thought_id === activeItem.thought_id
    ) {
      return true;
    }
    stack.push(...item.children);
  }
  return false;
}

type TimelineSubItemsProps = {
  items: Array<WorkflowRunTimelineItem>;
  activeItem: WorkflowRunOverviewActiveElement;
  activeIteration?: number | null;
  depth: number;
  blockOrder?: ReadonlyMap<string, number>;
  codeStepsByLabel?: ReadonlyMap<string, Array<CodeBlockStep>>;
  skippedBranchBlocksByConditionalId?: ReadonlyMap<
    string,
    Array<SkippedBranchGroup>
  >;
  onBlockItemClick: (block: WorkflowRunBlock) => void;
  onIterationClick?: (
    loopBlock: WorkflowRunBlock,
    iterationIndex: number,
  ) => void;
  onActionClick: (action: ActionItem) => void;
  onThoughtClick?: (thought: ObserverThought) => void;
  renderThoughts?: boolean;
  finallyBlockLabel?: string | null;
  workflowRunIsFinalized?: boolean;
};

function TimelineSubItems({
  items,
  activeItem,
  activeIteration = null,
  depth,
  blockOrder,
  codeStepsByLabel,
  skippedBranchBlocksByConditionalId,
  onBlockItemClick,
  onIterationClick,
  onActionClick,
  onThoughtClick,
  renderThoughts = false,
  finallyBlockLabel,
  workflowRunIsFinalized,
}: TimelineSubItemsProps) {
  return (
    <div>
      {items.map((item) => {
        if (isBlockItem(item)) {
          return (
            <WorkflowRunTimelineBlockItem
              key={item.block.workflow_run_block_id}
              subItems={item.children}
              activeItem={activeItem}
              activeIteration={activeIteration}
              block={item.block}
              depth={depth}
              blockOrder={blockOrder}
              codeStepsByLabel={codeStepsByLabel}
              skippedBranchBlocksByConditionalId={
                skippedBranchBlocksByConditionalId
              }
              onActionClick={onActionClick}
              onBlockItemClick={onBlockItemClick}
              onIterationClick={onIterationClick}
              onThoughtClick={onThoughtClick}
              renderThoughts={renderThoughts}
              finallyBlockLabel={finallyBlockLabel}
              workflowRunIsFinalized={workflowRunIsFinalized}
            />
          );
        }
        if (renderThoughts && isThoughtItem(item) && onThoughtClick) {
          return (
            <div key={item.thought.thought_id} className="py-1 pl-7">
              <ThoughtCard
                active={
                  isObserverThought(activeItem) &&
                  activeItem.thought_id === item.thought.thought_id
                }
                onClick={onThoughtClick}
                thought={item.thought}
              />
            </div>
          );
        }
        // Thoughts are no longer rendered as cards in the compact timeline
        // rail; the block detail panel surfaces them inside the owning
        // block's view alongside its actions.
        return null;
      })}
    </div>
  );
}

type TimelineActionRowsProps = {
  block: WorkflowRunBlock;
  activeItem: WorkflowRunOverviewActiveElement;
  depth: number;
  codeSteps: Array<CodeBlockStep>;
  onActionClick: (action: ActionItem) => void;
  workflowRunIsFinalized?: boolean;
};

function TimelineActionRows({
  block,
  activeItem,
  depth,
  codeSteps,
  onActionClick,
  workflowRunIsFinalized,
}: TimelineActionRowsProps) {
  const actions = block.actions ?? [];
  const actionsTopDown = [...actions].reverse();
  const isCodeBlock = block.block_type === WorkflowBlockTypes.Code;

  if (actions.length === 0) return null;

  return (
    <div className="space-y-1 py-1">
      {actionsTopDown.map((action, index) => {
        const isActive =
          isAction(activeItem) && activeItem.action_id === action.action_id;
        const displayIndex = index + 1;
        const { icon, label, summary, tone } = isCodeBlock
          ? getCodeActionRowPresentation(
              action,
              findCodeStepForLine(
                codeSteps,
                getRecordedActionMeta(action).codeLine,
              ),
            )
          : {
              icon: timelineActionIcons[action.action_type],
              label: getReadableActionType(action.action_type),
              summary: getActionSummary(action),
              tone: "default" as const,
            };

        return (
          <div
            key={action.action_id}
            className="flex min-h-[24px] items-stretch text-xs"
          >
            <IndentRails depth={depth} />
            <div
              className={cn(
                "flex min-w-0 flex-1 items-center gap-1.5 rounded-r py-0.5 pr-1.5",
                "hover:bg-muted/60",
                isActive && "bg-slate-elevation4 dark:bg-slate-800",
              )}
              style={railHighlightStyle}
            >
              <div className="size-4 shrink-0" />
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  onActionClick({ block, action });
                }}
                aria-pressed={isActive}
                className="flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 rounded text-left outline-none focus-visible:ring-1 focus-visible:ring-foreground/40"
              >
                <StatusDot
                  status={getActionDisplayStatus(action)}
                  isFinalized={!!workflowRunIsFinalized}
                />
                <span
                  className="shrink-0 text-muted-foreground"
                  aria-hidden="true"
                >
                  {icon}
                </span>
                <span className="w-7 shrink-0 text-[10px] tabular-nums text-muted-foreground dark:text-slate-500">
                  #{displayIndex}
                </span>
                <span
                  className={cn(
                    "shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-medium",
                    tone === "error"
                      ? "border-rose-500/30 bg-rose-500/10 text-rose-700 dark:text-rose-300"
                      : "border-border text-muted-foreground",
                  )}
                >
                  {label}
                </span>
                {summary && (
                  <span className="min-w-0 flex-1 truncate text-muted-foreground dark:text-slate-500">
                    · {summary}
                  </span>
                )}
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function formatCodeStepLines(step: CodeBlockStep): string | null {
  if (step.line_start == null) {
    return null;
  }
  if (step.line_end == null || step.line_end === step.line_start) {
    return `L${step.line_start}`;
  }
  return `L${step.line_start}-${step.line_end}`;
}

// The line where a code block stopped: use the synthetic error row's code line
// (failed null_action) when present, and avoid inferring skipped definition
// steps without that explicit failure marker.
function getCodeBlockFailureLine(
  actions: Array<ActionsApiResponse>,
): number | null {
  let errorLine: number | null = null;
  for (const action of actions) {
    const { codeLine } = getRecordedActionMeta(action);
    if (codeLine === null) continue;
    if (
      action.action_type === ActionTypes.NullAction &&
      action.status === Status.Failed
    ) {
      errorLine = errorLine === null ? codeLine : Math.max(errorLine, codeLine);
    }
  }
  return errorLine;
}

// Definition steps whose code position is strictly after the failure line never
// executed. Steps without a line position, or whose range starts at/before the
// failure, are excluded — they either ran or were in progress when it stopped.
function getUnfiredCodeSteps(
  steps: Array<CodeBlockStep>,
  actions: Array<ActionsApiResponse>,
): Array<CodeBlockStep> {
  const failureLine = getCodeBlockFailureLine(actions);
  if (failureLine === null) return [];
  return steps.filter(
    (step) => step.line_start != null && step.line_start > failureLine,
  );
}

type TimelineCodeStepRowsProps = {
  block: WorkflowRunBlock;
  steps: Array<CodeBlockStep>;
  depth: number;
  onBlockItemClick: (block: WorkflowRunBlock) => void;
};

// Code blocks have no recorded actions to enumerate, so surface their
// definition step outline beneath the block instead.
function TimelineCodeStepRows({
  block,
  steps,
  depth,
  onBlockItemClick,
}: TimelineCodeStepRowsProps) {
  if (steps.length === 0) return null;

  return (
    <div className="space-y-1 py-1">
      {steps.map((step, index) => {
        const lines = formatCodeStepLines(step);
        const summary =
          normalizeInlineText(step.title) ??
          normalizeInlineText(step.description);

        return (
          <div key={index} className="flex min-h-[24px] items-stretch text-xs">
            <IndentRails depth={depth} />
            <div
              className={cn(
                "flex min-w-0 flex-1 items-center gap-1.5 rounded-r py-0.5 pr-1.5",
                "hover:bg-muted/60",
              )}
              style={railHighlightStyle}
            >
              <div className="size-4 shrink-0" />
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  onBlockItemClick(block);
                }}
                className="flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 rounded text-left outline-none focus-visible:ring-1 focus-visible:ring-foreground/40"
              >
                <span className="w-7 shrink-0 text-[10px] tabular-nums text-muted-foreground dark:text-slate-500">
                  #{index + 1}
                </span>
                <span className="shrink-0 rounded border border-border px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
                  {getReadableActionType(step.action_type)}
                </span>
                {summary && (
                  <span className="min-w-0 flex-1 truncate text-muted-foreground dark:text-slate-500">
                    · {summary}
                  </span>
                )}
                {lines && (
                  <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground dark:text-slate-500">
                    {lines}
                  </span>
                )}
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

type TimelineSkippedStepRowsProps = {
  block: WorkflowRunBlock;
  steps: Array<CodeBlockStep>;
  depth: number;
  onBlockItemClick: (block: WorkflowRunBlock) => void;
};

// Negative-space sibling of TimelineActionRows: when a code block fails partway,
// its definition steps after the failure point never ran. Mirror the block-level
// "didn't run" treatment (WorkflowRunTimelineUnexecutedBlockItem) one level down
// — dimmed, hollow dashed marker, neutral slate (never the rose error tone).
function TimelineSkippedStepRows({
  block,
  steps,
  depth,
  onBlockItemClick,
}: TimelineSkippedStepRowsProps) {
  if (steps.length === 0) return null;

  return (
    <div className="space-y-1 pb-1">
      {steps.map((step, index) => {
        const lines = formatCodeStepLines(step);
        const summary =
          normalizeInlineText(step.title) ??
          normalizeInlineText(step.description);

        return (
          <div
            key={`skipped-${index}`}
            className="flex min-h-[24px] items-stretch text-xs opacity-60"
          >
            <IndentRails depth={depth} />
            <div
              className={cn(
                "flex min-w-0 flex-1 items-center gap-1.5 rounded-r py-0.5 pr-1.5",
                "hover:bg-muted/60",
              )}
              style={railHighlightStyle}
            >
              <div className="size-4 shrink-0" />
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  onBlockItemClick(block);
                }}
                className="flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 rounded text-left outline-none focus-visible:ring-1 focus-visible:ring-foreground/40"
              >
                <span
                  className="size-2 shrink-0 rounded-full border border-dashed border-border dark:border-slate-500"
                  aria-hidden="true"
                />
                <span className="shrink-0 rounded border border-border px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
                  {getReadableActionType(step.action_type)}
                </span>
                {summary && (
                  <span className="min-w-0 flex-1 truncate text-muted-foreground dark:text-slate-500">
                    · {summary}
                  </span>
                )}
                {lines && (
                  <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground dark:text-slate-500">
                    {lines}
                  </span>
                )}
                <span
                  className="ml-auto shrink-0 rounded bg-muted px-1 text-[10px] uppercase tracking-wide text-muted-foreground"
                  title="This step did not execute because the code block stopped before reaching it."
                >
                  didn't run
                </span>
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

type TimelineSkippedBranchRowsProps = {
  groups: Array<SkippedBranchGroup>;
  depth: number;
};

function getSkippedBranchLabel(branch: SkippedBranchMetadata): string {
  if (branch.description) {
    return branch.description;
  }
  if (branch.criteriaDescription) {
    return branch.criteriaDescription;
  }
  return branch.isDefault
    ? `Default target ${branch.nextBlockLabel}`
    : `Target ${branch.nextBlockLabel}`;
}

function getExcelStyleLetter(index: number): string {
  let result = "";
  let num = index;

  while (num >= 0) {
    result = String.fromCharCode(65 + (num % 26)) + result;
    num = Math.floor(num / 26) - 1;
  }

  return result;
}

function getSkippedBranchMarker(branch: SkippedBranchMetadata): string {
  if (branch.branchIndex === null) {
    return branch.isDefault ? "Else" : "Branch";
  }

  const letter = getExcelStyleLetter(branch.branchIndex);
  if (branch.isDefault) {
    return `${letter} • Else`;
  }
  if (branch.branchIndex === 0) {
    return `${letter} • If`;
  }
  return `${letter} • Else If`;
}

function getSkippedBranchReason(branch: SkippedBranchMetadata): string {
  if (branch.error) return "eval error";
  if (branch.result === false) return "condition false";
  if (branch.wasEvaluated && branch.result === null) return "no match";
  if (branch.isDefault) return "another branch matched";
  if (!branch.wasEvaluated) return "not evaluated";
  return "another branch matched";
}

function getSkippedBranchTitle(branch: SkippedBranchMetadata): string {
  const parts = [
    `Skipped ${getSkippedBranchMarker(branch)}: ${getSkippedBranchLabel(branch)}`,
    `Reason: ${getSkippedBranchReason(branch)}`,
    `Target: ${branch.nextBlockLabel}`,
  ];
  if (branch.expression) {
    parts.push(`Expression: ${branch.expression}`);
  }
  if (
    branch.renderedExpression &&
    branch.renderedExpression !== branch.expression
  ) {
    parts.push(`Rendered: ${branch.renderedExpression}`);
  }
  if (branch.error) {
    parts.push(`Error: ${branch.error}`);
  }
  return parts.join("\n");
}

function TimelineSkippedBranchRows({
  groups,
  depth,
}: TimelineSkippedBranchRowsProps) {
  if (groups.length === 0) return null;

  return (
    <div className="space-y-1 py-1">
      {groups.map((group) => (
        <TimelineSkippedBranchGroup
          key={group.key}
          group={group}
          depth={depth}
        />
      ))}
    </div>
  );
}

function TimelineSkippedBranchGroup({
  group,
  depth,
}: {
  group: SkippedBranchGroup;
  depth: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const branchMarker = getSkippedBranchMarker(group.branch);
  const branchLabel = getSkippedBranchLabel(group.branch);
  const branchReason = getSkippedBranchReason(group.branch);
  const title = getSkippedBranchTitle(group.branch);

  return (
    <div>
      <div className="flex min-h-[24px] items-stretch text-xs opacity-70">
        <IndentRails depth={depth} />
        <div
          className="flex min-w-0 flex-1 items-center gap-1.5 rounded-r py-0.5 pr-1.5 text-muted-foreground"
          style={railHighlightStyle}
          title={title}
        >
          <button
            type="button"
            className="inline-flex size-4 shrink-0 items-center justify-center rounded text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-1 focus-visible:ring-foreground/40 dark:hover:bg-slate-700 dark:hover:text-slate-200"
            onClick={(event) => {
              event.stopPropagation();
              setExpanded((prev) => !prev);
            }}
            aria-label={
              expanded ? "Collapse skipped branch" : "Expand skipped branch"
            }
            aria-expanded={expanded}
          >
            {expanded ? (
              <ChevronDownIcon className="size-4" />
            ) : (
              <ChevronRightIcon className="size-4" />
            )}
          </button>
          <div className="flex min-w-0 flex-1 items-center gap-1.5">
            <span
              className="size-2 shrink-0 rounded-full border border-dashed border-border dark:border-slate-500"
              aria-hidden="true"
            />
            <span className="shrink-0 rounded border border-border bg-muted/50 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
              {branchMarker}
            </span>
            <span className="min-w-0 flex-1 truncate text-muted-foreground dark:text-slate-500">
              · {branchLabel}
            </span>
            <span className="shrink-0 rounded bg-muted/60 px-1 text-[10px] text-muted-foreground">
              {branchReason}
            </span>
            <span className="shrink-0 rounded bg-muted px-1 text-[10px] tabular-nums text-muted-foreground">
              {group.blocks.length}{" "}
              {group.blocks.length === 1 ? "block" : "blocks"}
            </span>
          </div>
        </div>
      </div>
      <Collapsible open={expanded}>
        <CollapsibleContent className="overflow-hidden motion-safe:data-[state=closed]:animate-collapsible-up-fade motion-safe:data-[state=open]:animate-collapsible-down-fade">
          {group.blocks.map(({ block, reason }) => (
            <WorkflowRunTimelineUnexecutedBlockItem
              key={`skipped-branch-${block.label}`}
              block={block}
              depth={depth + 1}
              reason={reason}
            />
          ))}
        </CollapsibleContent>
      </Collapsible>
    </div>
  );
}

function WorkflowRunTimelineBlockItem({
  activeItem,
  activeIteration = null,
  block,
  blockOrder,
  codeStepsByLabel,
  skippedBranchBlocksByConditionalId,
  subItems,
  depth = 0,
  onBlockItemClick,
  onIterationClick,
  onActionClick,
  onThoughtClick,
  renderThoughts = false,
  finallyBlockLabel,
  workflowRunIsFinalized = false,
}: Props) {
  const isFinallyBlock = finallyBlockLabel && block.label === finallyBlockLabel;
  const isRunning = block.status === Status.Running && !workflowRunIsFinalized;
  const duration =
    block.duration !== null ? formatDuration(toDuration(block.duration)) : null;
  const blockTypeTitle = workflowBlockTitle[block.block_type];
  const blockTypeLabel = getTimelineTypeLabel(block);
  const blockIndex = blockOrder?.get(block.workflow_run_block_id);
  const actions = block.actions ?? [];
  const actionCount = actions.length;

  const hasActions = actionCount > 0;
  const isCodeBlock = block.block_type === WorkflowBlockTypes.Code;
  const definitionCodeSteps = isCodeBlock
    ? (codeStepsByLabel?.get(block.label ?? "") ?? [])
    : [];
  const blockName = isCodeBlock
    ? getCodeBlockTimelineName(block, definitionCodeSteps)
    : (block.label ?? block.title ?? blockTypeTitle);
  const descriptor = isCodeBlock
    ? (block.label ?? "Code block")
    : getTimelineDescriptor(block);
  const showsActionRows = hasActions;
  // Code blocks without recorded actions fall back to their definition step
  // outline so the timeline still reflects what the block was meant to do.
  const codeSteps = isCodeBlock && !hasActions ? definitionCodeSteps : [];
  const hasCodeSteps = codeSteps.length > 0;
  // When a code block fails partway, the definition steps after the failure
  // point left no action record — surface them as dimmed "didn't run" rows so
  // the timeline shows what never executed, not just what did.
  const blockFailed =
    block.status === Status.Failed ||
    block.status === Status.Terminated ||
    block.status === Status.TimedOut ||
    block.status === Status.Canceled;
  const skippedCodeSteps =
    isCodeBlock && blockFailed && hasActions
      ? getUnfiredCodeSteps(definitionCodeSteps, actions)
      : [];
  const hasSkippedCodeSteps = skippedCodeSteps.length > 0;
  const skippedBranchGroups =
    block.block_type === "conditional"
      ? (skippedBranchBlocksByConditionalId?.get(block.workflow_run_block_id) ??
        [])
      : [];
  const hasSkippedBranchBlocks = skippedBranchGroups.length > 0;
  const isForLoopBlock = block.block_type === "for_loop";
  const isWhileLoopBlock = block.block_type === "while_loop";
  const isLoopBlock = isForLoopBlock || isWhileLoopBlock;
  const loopIterationGroups = useMemo(
    () => (isLoopBlock ? getLoopIterationGroups(subItems) : []),
    [isLoopBlock, subItems],
  );
  const hasRenderableNestedChildren = subItems.some(
    (item) => isBlockItem(item) || (renderThoughts && isThoughtItem(item)),
  );
  // Only treat as a container when there are actual children to reveal.
  // Conditionals and loops can be defined as containers structurally, but if
  // the runtime didn't model any child blocks under them (e.g. conditionals
  // whose "next" block is a flat sibling), showing a chevron that reveals
  // nothing is worse than no chevron at all.
  const isContainer =
    hasRenderableNestedChildren ||
    showsActionRows ||
    hasCodeSteps ||
    hasSkippedBranchBlocks;

  // The loop block itself is only "active" when no specific iteration is
  // selected — otherwise the iteration row owns the highlight.
  //
  // When an action inside this block is the selection, the owning block stays
  // highlighted — mirrors the loop-iteration pattern so the user never loses
  // the parent context after drilling into Panel B's action cards.
  const ownsSelectedAction =
    isAction(activeItem) &&
    (block.actions ?? []).some((a) => a.action_id === activeItem.action_id);
  const hasResolvedActiveIteration =
    activeIteration !== null &&
    loopIterationGroups.some((group) => group.index === activeIteration);
  const isActiveBlock =
    ((isWorkflowRunBlock(activeItem) &&
      activeItem.workflow_run_block_id === block.workflow_run_block_id) ||
      ownsSelectedAction) &&
    !(isLoopBlock && hasResolvedActiveIteration);
  const hasActiveDescendant = useMemo(
    () => timelineItemsContainActiveElement(subItems, activeItem),
    [subItems, activeItem],
  );
  // Deep-link case: `?active=<loopId>&iteration=N`. The loop block is the
  // selected item, but `isActiveBlock` is intentionally suppressed (the
  // iteration row owns the highlight) and `hasActiveDescendant` is false
  // (the loop isn't its own child). Without this, the loop stays collapsed
  // and the targeted iteration row is hidden.
  const isLoopWithSelectedIteration =
    isLoopBlock &&
    hasResolvedActiveIteration &&
    isWorkflowRunBlock(activeItem) &&
    activeItem.workflow_run_block_id === block.workflow_run_block_id;

  const [expanded, setExpanded] = useState(
    isRunning ||
      isActiveBlock ||
      showsActionRows ||
      hasSkippedBranchBlocks ||
      hasActiveDescendant ||
      isLoopWithSelectedIteration ||
      !hasRenderableNestedChildren,
  );
  const userToggledRef = useRef(false);

  useEffect(() => {
    userToggledRef.current = false;
  }, [block.workflow_run_block_id]);

  // Auto-expand when actions appear, an active descendant appears, or this
  // block starts running.
  // Skip once the user has explicitly toggled the chevron — their choice wins.
  useEffect(() => {
    if (userToggledRef.current) return;
    if (
      showsActionRows ||
      hasSkippedBranchBlocks ||
      hasActiveDescendant ||
      isRunning ||
      isLoopWithSelectedIteration
    ) {
      setExpanded(true);
    }
  }, [
    showsActionRows,
    hasSkippedBranchBlocks,
    hasActiveDescendant,
    isRunning,
    isLoopWithSelectedIteration,
  ]);

  const loopValues = Array.isArray(block.loop_values) ? block.loop_values : [];

  // Loop inline counter (e.g. 3/8).
  const loopCounter = isForLoopBlock
    ? loopValues.length > 0
      ? `${loopIterationGroups.length}/${loopValues.length}`
      : null
    : isWhileLoopBlock
      ? `${loopIterationGroups.length}`
      : null;

  return (
    <div className="min-w-0">
      <div className="flex min-h-[28px] items-stretch text-xs">
        <IndentRails depth={depth} />
        <div
          className={cn(
            "flex min-w-0 flex-1 items-center gap-1.5 rounded-r py-1 pr-1.5",
            "hover:bg-muted/60",
            isActiveBlock && "bg-slate-elevation4 dark:bg-slate-800",
          )}
          style={railHighlightStyle}
        >
          {isContainer ? (
            <button
              type="button"
              className="inline-flex size-4 shrink-0 items-center justify-center rounded text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-1 focus-visible:ring-foreground/40 dark:hover:bg-slate-700 dark:hover:text-slate-200"
              onClick={(event) => {
                event.stopPropagation();
                userToggledRef.current = true;
                setExpanded((prev) => !prev);
              }}
              aria-label={expanded ? "Collapse" : "Expand"}
              aria-expanded={expanded}
            >
              {expanded ? (
                <ChevronDownIcon className="size-4" />
              ) : (
                <ChevronRightIcon className="size-4" />
              )}
            </button>
          ) : (
            <div className="size-4 shrink-0" />
          )}
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              onBlockItemClick(block);
            }}
            aria-pressed={isActiveBlock}
            className="flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 rounded text-left outline-none focus-visible:ring-1 focus-visible:ring-foreground/40"
          >
            <StatusDot
              status={block.status}
              isFinalized={!!workflowRunIsFinalized}
            />
            <span title={blockTypeTitle} className="shrink-0">
              <WorkflowBlockIcon
                workflowBlockType={block.block_type}
                className="size-3.5 text-tertiary-foreground"
              />
            </span>
            {blockIndex !== undefined && (
              <span className="w-7 shrink-0 text-[10px] tabular-nums text-muted-foreground dark:text-slate-500">
                #{blockIndex}
              </span>
            )}
            <span className="inline-flex min-w-[6rem] max-w-[8rem] shrink-0 justify-center truncate rounded bg-muted/70 px-1.5 py-0.5 text-[10px] font-medium text-tertiary-foreground dark:bg-slate-700/70">
              {blockTypeLabel}
            </span>
            <span className="min-w-0 max-w-[12rem] truncate text-foreground dark:text-slate-200">
              {blockName}
            </span>
            <span className="min-w-0 flex-1 truncate text-muted-foreground dark:text-slate-500">
              · {descriptor}
            </span>
            {isFinallyBlock && (
              <span className="shrink-0 rounded bg-amber-500/80 px-1 text-[9px] font-medium text-black">
                finally
              </span>
            )}
            {hasActions && (
              <span className="shrink-0 rounded bg-muted px-1 text-[10px] tabular-nums text-muted-foreground">
                {actionCount} {actionCount === 1 ? "action" : "actions"}
              </span>
            )}
            {loopCounter && (
              <span className="shrink-0 rounded bg-muted px-1 text-[10px] tabular-nums text-tertiary-foreground dark:bg-slate-700">
                {loopCounter}
              </span>
            )}
            {duration && (
              <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground dark:text-slate-500">
                {duration}
              </span>
            )}
          </button>
        </div>
      </div>

      {/* Container body — always mounted so open/close transitions animate */}
      {isContainer && (
        <Collapsible open={expanded}>
          <CollapsibleContent className="overflow-hidden motion-safe:data-[state=closed]:animate-collapsible-up-fade motion-safe:data-[state=open]:animate-collapsible-down-fade">
            {showsActionRows && (
              <TimelineActionRows
                block={block}
                activeItem={activeItem}
                depth={depth + 1}
                codeSteps={definitionCodeSteps}
                onActionClick={onActionClick}
                workflowRunIsFinalized={workflowRunIsFinalized}
              />
            )}
            {hasCodeSteps && (
              <TimelineCodeStepRows
                block={block}
                steps={codeSteps}
                depth={depth + 1}
                onBlockItemClick={onBlockItemClick}
              />
            )}
            {hasSkippedCodeSteps && (
              <TimelineSkippedStepRows
                block={block}
                steps={skippedCodeSteps}
                depth={depth + 1}
                onBlockItemClick={onBlockItemClick}
              />
            )}
            {hasSkippedBranchBlocks && (
              <TimelineSkippedBranchRows
                groups={skippedBranchGroups}
                depth={depth + 1}
              />
            )}
            {isLoopBlock && loopIterationGroups.length > 0 ? (
              <LoopIterationRows
                loopBlock={block}
                groups={loopIterationGroups}
                activeItem={activeItem}
                activeIteration={activeIteration}
                depth={depth + 1}
                blockOrder={blockOrder}
                codeStepsByLabel={codeStepsByLabel}
                skippedBranchBlocksByConditionalId={
                  skippedBranchBlocksByConditionalId
                }
                onBlockItemClick={onBlockItemClick}
                onIterationClick={onIterationClick}
                onActionClick={onActionClick}
                onThoughtClick={onThoughtClick}
                renderThoughts={renderThoughts}
                finallyBlockLabel={finallyBlockLabel}
                workflowRunIsFinalized={workflowRunIsFinalized}
              />
            ) : (
              hasRenderableNestedChildren && (
                <TimelineSubItems
                  items={subItems}
                  activeItem={activeItem}
                  activeIteration={activeIteration}
                  depth={depth + 1}
                  blockOrder={blockOrder}
                  codeStepsByLabel={codeStepsByLabel}
                  skippedBranchBlocksByConditionalId={
                    skippedBranchBlocksByConditionalId
                  }
                  onActionClick={onActionClick}
                  onBlockItemClick={onBlockItemClick}
                  onIterationClick={onIterationClick}
                  onThoughtClick={onThoughtClick}
                  renderThoughts={renderThoughts}
                  finallyBlockLabel={finallyBlockLabel}
                  workflowRunIsFinalized={workflowRunIsFinalized}
                />
              )
            )}
          </CollapsibleContent>
        </Collapsible>
      )}
    </div>
  );
}

type LoopIterationRowsProps = {
  loopBlock: WorkflowRunBlock;
  groups: Array<LoopIterationGroup>;
  activeItem: WorkflowRunOverviewActiveElement;
  activeIteration?: number | null;
  depth: number;
  blockOrder?: ReadonlyMap<string, number>;
  codeStepsByLabel?: ReadonlyMap<string, Array<CodeBlockStep>>;
  skippedBranchBlocksByConditionalId?: ReadonlyMap<
    string,
    Array<SkippedBranchGroup>
  >;
  onBlockItemClick: (block: WorkflowRunBlock) => void;
  onIterationClick?: (
    loopBlock: WorkflowRunBlock,
    iterationIndex: number,
  ) => void;
  onActionClick: (action: ActionItem) => void;
  onThoughtClick?: (thought: ObserverThought) => void;
  renderThoughts?: boolean;
  finallyBlockLabel?: string | null;
  workflowRunIsFinalized?: boolean;
};

function LoopIterationRows({
  loopBlock,
  groups,
  activeItem,
  activeIteration = null,
  depth,
  blockOrder,
  codeStepsByLabel,
  skippedBranchBlocksByConditionalId,
  onBlockItemClick,
  onIterationClick,
  onActionClick,
  onThoughtClick,
  renderThoughts = false,
  finallyBlockLabel,
  workflowRunIsFinalized,
}: LoopIterationRowsProps) {
  return (
    <div>
      {groups.map((group, groupIndex) => (
        // Key by group.index alone so existing iteration rows keep their
        // identity (and useState) when a new iteration arrives at the top
        // of the DESC-sorted list. Including groupIndex would shift every
        // existing row's key and remount the whole stack on each update.
        <LoopIterationRow
          key={group.index ?? "unknown"}
          loopBlock={loopBlock}
          group={group}
          groupIndex={groupIndex}
          groupCount={groups.length}
          activeItem={activeItem}
          activeIteration={activeIteration}
          depth={depth}
          blockOrder={blockOrder}
          codeStepsByLabel={codeStepsByLabel}
          skippedBranchBlocksByConditionalId={
            skippedBranchBlocksByConditionalId
          }
          onBlockItemClick={onBlockItemClick}
          onIterationClick={onIterationClick}
          onActionClick={onActionClick}
          onThoughtClick={onThoughtClick}
          renderThoughts={renderThoughts}
          finallyBlockLabel={finallyBlockLabel}
          workflowRunIsFinalized={workflowRunIsFinalized}
        />
      ))}
    </div>
  );
}

type LoopIterationRowProps = {
  loopBlock: WorkflowRunBlock;
  group: LoopIterationGroup;
  groupIndex: number;
  groupCount: number;
  activeItem: WorkflowRunOverviewActiveElement;
  activeIteration?: number | null;
  depth: number;
  blockOrder?: ReadonlyMap<string, number>;
  codeStepsByLabel?: ReadonlyMap<string, Array<CodeBlockStep>>;
  skippedBranchBlocksByConditionalId?: ReadonlyMap<
    string,
    Array<SkippedBranchGroup>
  >;
  onBlockItemClick: (block: WorkflowRunBlock) => void;
  onIterationClick?: (
    loopBlock: WorkflowRunBlock,
    iterationIndex: number,
  ) => void;
  onActionClick: (action: ActionItem) => void;
  onThoughtClick?: (thought: ObserverThought) => void;
  renderThoughts?: boolean;
  finallyBlockLabel?: string | null;
  workflowRunIsFinalized?: boolean;
};

function LoopIterationRow({
  loopBlock,
  group,
  groupIndex,
  groupCount,
  activeItem,
  activeIteration = null,
  depth,
  blockOrder,
  codeStepsByLabel,
  skippedBranchBlocksByConditionalId,
  onBlockItemClick,
  onIterationClick,
  onActionClick,
  onThoughtClick,
  renderThoughts = false,
  finallyBlockLabel,
  workflowRunIsFinalized,
}: LoopIterationRowProps) {
  const hasActiveDescendant = useMemo(
    () => timelineItemsContainActiveElement(group.items, activeItem),
    [group.items, activeItem],
  );
  const status = useMemo(
    () => aggregateIterationStatus(group.items),
    [group.items],
  );

  // Default open: latest group, running, or has active.
  const [expanded, setExpanded] = useState(
    groupIndex === groupCount - 1 ||
      hasActiveDescendant ||
      status === Status.Running,
  );
  const userToggledRef = useRef(false);

  useEffect(() => {
    userToggledRef.current = false;
  }, [loopBlock.workflow_run_block_id, group.index]);

  // Mirror the block-row pattern: auto-expand when status flips to running
  // or an active descendant appears, unless the user has explicitly
  // collapsed this row.
  useEffect(() => {
    if (userToggledRef.current) return;
    if (hasActiveDescendant || status === Status.Running) {
      setExpanded(true);
    }
  }, [hasActiveDescendant, status]);

  const iterationIndex = group.index !== null ? group.index : groupIndex;
  const iterationNumber = iterationIndex + 1;
  const currentValuePreview = normalizeInlineText(group.currentValue);
  const isActiveIteration =
    isWorkflowRunBlock(activeItem) &&
    activeItem.workflow_run_block_id === loopBlock.workflow_run_block_id &&
    activeIteration === iterationIndex;

  return (
    <div className="min-w-0">
      <div className="flex min-h-[24px] items-stretch text-[11px] text-tertiary-foreground">
        <IndentRails depth={depth} />
        <div
          className={cn(
            "flex min-w-0 flex-1 items-center gap-1.5 rounded-r py-0.5 pr-1.5",
            "hover:bg-muted/60",
            isActiveIteration && "bg-slate-elevation4 dark:bg-slate-800",
          )}
          style={railHighlightStyle}
        >
          <button
            type="button"
            className="-ml-0.5 size-3.5 shrink-0 rounded text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-1 focus-visible:ring-foreground/40 dark:text-slate-500 dark:hover:bg-slate-700 dark:hover:text-slate-200"
            onClick={(event) => {
              event.stopPropagation();
              userToggledRef.current = true;
              setExpanded((prev) => !prev);
            }}
            aria-label={expanded ? "Collapse iteration" : "Expand iteration"}
            aria-expanded={expanded}
          >
            {expanded ? (
              <ChevronDownIcon className="size-3.5" />
            ) : (
              <ChevronRightIcon className="size-3.5" />
            )}
          </button>
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              if (onIterationClick) {
                onIterationClick(loopBlock, iterationIndex);
              } else {
                setExpanded((prev) => !prev);
              }
            }}
            aria-pressed={isActiveIteration}
            className="flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 rounded text-left outline-none focus-visible:ring-1 focus-visible:ring-foreground/40"
          >
            <StatusDot status={status} isFinalized={!!workflowRunIsFinalized} />
            <span className="shrink-0 text-muted-foreground">
              Iteration {iterationNumber}
            </span>
            {currentValuePreview && (
              <span className="min-w-0 flex-1 truncate text-muted-foreground dark:text-slate-500">
                · {currentValuePreview}
              </span>
            )}
          </button>
        </div>
      </div>
      <Collapsible open={expanded}>
        <CollapsibleContent className="overflow-hidden motion-safe:data-[state=closed]:animate-collapsible-up-fade motion-safe:data-[state=open]:animate-collapsible-down-fade">
          <TimelineSubItems
            items={group.items}
            activeItem={activeItem}
            activeIteration={activeIteration}
            depth={depth + 1}
            blockOrder={blockOrder}
            codeStepsByLabel={codeStepsByLabel}
            skippedBranchBlocksByConditionalId={
              skippedBranchBlocksByConditionalId
            }
            onActionClick={onActionClick}
            onBlockItemClick={onBlockItemClick}
            onIterationClick={onIterationClick}
            onThoughtClick={onThoughtClick}
            renderThoughts={renderThoughts}
            finallyBlockLabel={finallyBlockLabel}
            workflowRunIsFinalized={workflowRunIsFinalized}
          />
        </CollapsibleContent>
      </Collapsible>
    </div>
  );
}

export { WorkflowRunTimelineBlockItem };
export type { SkippedBranchGroup };
