import {
  CheckCircledIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  CrossCircledIcon,
  CubeIcon,
  ExternalLinkIcon,
} from "@radix-ui/react-icons";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { Status } from "@/api/types";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { formatDuration, toDuration } from "@/routes/workflows/utils";
import { cn } from "@/util/utils";
import { workflowBlockTitle } from "../editor/nodes/types";
import { WorkflowBlockIcon } from "../editor/nodes/WorkflowBlockIcon";
import {
  hasEvaluations,
  isAction,
  isBlockItem,
  isObserverThought,
  isThoughtItem,
  isWorkflowRunBlock,
  ObserverThought,
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";
import { isTaskVariantBlock } from "../types/workflowTypes";
import { ActionCard } from "./ActionCard";
import { WorkflowRunHumanInteraction } from "./WorkflowRunHumanInteraction";
import {
  ActionItem,
  WorkflowRunOverviewActiveElement,
} from "./WorkflowRunOverview";
import { ThoughtCard } from "./ThoughtCard";

type Props = {
  activeItem: WorkflowRunOverviewActiveElement;
  block: WorkflowRunBlock;
  subItems: Array<WorkflowRunTimelineItem>;
  depth?: number;
  onBlockItemClick: (block: WorkflowRunBlock) => void;
  onActionClick: (action: ActionItem) => void;
  onThoughtCardClick: (thought: ObserverThought) => void;
  finallyBlockLabel?: string | null;
};

type LoopIterationGroup = {
  index: number | null;
  currentValue: string | null;
  items: Array<WorkflowRunTimelineItem>;
};

function stringifyTimelineValue(value: unknown): string {
  if (value === null || value === undefined) {
    return "null";
  }

  if (
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  ) {
    return String(value);
  }

  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function truncateValue(value: string, maxLength = 120): string {
  const collapsed = value.replace(/\s+/g, " ").trim();
  if (collapsed.length <= maxLength) {
    return collapsed;
  }
  return `${collapsed.slice(0, maxLength - 3)}...`;
}

function getLoopIterationGroups(
  items: Array<WorkflowRunTimelineItem>,
): Array<LoopIterationGroup> {
  const groupsByKey = new Map<string, LoopIterationGroup>();

  items.forEach((item) => {
    const currentIndex = isBlockItem(item) ? item.block.current_index : null;
    const currentValue = isBlockItem(item) ? item.block.current_value : null;
    const groupKey =
      currentIndex === null ? "unknown" : `index-${currentIndex}`;

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

  return Array.from(groupsByKey.values()).sort((left, right) => {
    if (left.index === null && right.index === null) {
      return 0;
    }
    if (left.index === null) {
      return 1;
    }
    if (right.index === null) {
      return -1;
    }
    return right.index - left.index;
  });
}

/**
 * Check whether any block, action, or thought within `items` (recursively)
 * matches the currently-active element.  Uses an iterative stack to avoid
 * deep nesting — same pattern as `findActiveItem` in workflowTimelineUtils.
 */
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
  depth: number;
  onBlockItemClick: (block: WorkflowRunBlock) => void;
  onActionClick: (action: ActionItem) => void;
  onThoughtCardClick: (thought: ObserverThought) => void;
  finallyBlockLabel?: string | null;
};

function TimelineSubItems({
  items,
  activeItem,
  depth,
  onBlockItemClick,
  onActionClick,
  onThoughtCardClick,
  finallyBlockLabel,
}: TimelineSubItemsProps) {
  return (
    <div className="space-y-3">
      {items.map((item) => {
        if (isBlockItem(item)) {
          return (
            <WorkflowRunTimelineBlockItem
              key={item.block.workflow_run_block_id}
              subItems={item.children}
              activeItem={activeItem}
              block={item.block}
              depth={depth}
              onActionClick={onActionClick}
              onBlockItemClick={onBlockItemClick}
              onThoughtCardClick={onThoughtCardClick}
              finallyBlockLabel={finallyBlockLabel}
            />
          );
        }

        if (isThoughtItem(item)) {
          return (
            <ThoughtCard
              key={item.thought.thought_id}
              active={
                isObserverThought(activeItem) &&
                activeItem.thought_id === item.thought.thought_id
              }
              onClick={onThoughtCardClick}
              thought={item.thought}
            />
          );
        }
      })}
    </div>
  );
}

function WorkflowRunTimelineBlockItem({
  activeItem,
  block,
  subItems,
  depth = 0,
  onBlockItemClick,
  onActionClick,
  onThoughtCardClick,
  finallyBlockLabel,
}: Props) {
  const actions = block.actions ?? [];
  const isFinallyBlock = finallyBlockLabel && block.label === finallyBlockLabel;

  const hasActiveAction =
    isAction(activeItem) &&
    Boolean(
      block.actions?.find(
        (action) => action.action_id === activeItem.action_id,
      ),
    );
  const isActiveBlock =
    isWorkflowRunBlock(activeItem) &&
    activeItem.workflow_run_block_id === block.workflow_run_block_id;

  const showDiagnosticLink =
    isTaskVariantBlock(block) && (hasActiveAction || isActiveBlock);

  const refCallback = useCallback((element: HTMLDivElement | null) => {
    if (
      element &&
      isWorkflowRunBlock(activeItem) &&
      activeItem.workflow_run_block_id === block.workflow_run_block_id
    ) {
      element.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }
    // this should only run once at mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const showStatusIndicator = block.status !== null;

  const showSuccessIndicator =
    showStatusIndicator && block.status === Status.Completed;
  const showFailureIndicator =
    showStatusIndicator &&
    (block.status === Status.Failed ||
      block.status === Status.Terminated ||
      block.status === Status.TimedOut ||
      block.status === Status.Canceled);

  const duration =
    block.duration !== null ? formatDuration(toDuration(block.duration)) : null;

  // NOTE(jdo): want to put this back; await for now
  const showDuration = false as const;
  const hasNestedChildren = subItems.length > 0;
  const isLoopBlock = block.block_type === "for_loop";
  const isConditionalBlock = block.block_type === "conditional";
  const [childrenOpen, setChildrenOpen] = useState(true);

  const loopIterationGroups = useMemo(
    () => getLoopIterationGroups(subItems),
    [subItems],
  );

  // Track which loop iteration groups are expanded (controlled state).
  // Initialise with the most-recent group (index 0) plus any group that
  // contains the currently-active element so it is visible on first render.
  const [openLoopGroups, setOpenLoopGroups] = useState<Set<number>>(() => {
    const initial = new Set<number>();
    if (loopIterationGroups.length > 0) {
      initial.add(0);
    }
    loopIterationGroups.forEach((group, idx) => {
      if (timelineItemsContainActiveElement(group.items, activeItem)) {
        initial.add(idx);
      }
    });
    return initial;
  });

  // When the active element changes (or loop groups load for the first time),
  // auto-expand the group that contains it. Also ensure the most-recent group
  // (index 0) is open — this covers the edge case where subItems loads after
  // the initial mount so the useState initializer missed it.
  useEffect(() => {
    setOpenLoopGroups((prev) => {
      let next = prev;
      // Ensure most-recent group is open (covers late-loading data).
      if (loopIterationGroups.length > 0 && !prev.has(0)) {
        next = new Set(next);
        next.add(0);
      }
      // Find and expand the group containing the active item.
      for (let idx = 0; idx < loopIterationGroups.length; idx++) {
        const group = loopIterationGroups[idx];
        if (
          group &&
          timelineItemsContainActiveElement(group.items, activeItem)
        ) {
          if (!next.has(idx)) {
            if (next === prev) next = new Set(next);
            next.add(idx);
          }
          break; // active item can only be in one group
        }
      }
      return next;
    });
  }, [activeItem, loopIterationGroups]);

  // Auto-expand conditional children when the active element is inside them.
  useEffect(() => {
    if (
      isConditionalBlock &&
      hasNestedChildren &&
      timelineItemsContainActiveElement(subItems, activeItem)
    ) {
      setChildrenOpen(true);
    }
  }, [activeItem, isConditionalBlock, hasNestedChildren, subItems]);

  const loopValues = Array.isArray(block.loop_values) ? block.loop_values : [];

  return (
    <div
      className={cn({
        "ml-3 pl-3": depth > 0,
        "border-l border-slate-700": depth === 1,
      })}
    >
      <div
        className={cn(
          "cursor-pointer space-y-4 rounded border border-slate-600 p-4",
          {
            "border-slate-50":
              isWorkflowRunBlock(activeItem) &&
              activeItem.workflow_run_block_id === block.workflow_run_block_id,
          },
        )}
        onClick={(event) => {
          event.stopPropagation();
          onBlockItemClick(block);
        }}
        ref={refCallback}
      >
        <div className="space-y-2">
          <div className="flex justify-between">
            <div className="flex gap-3">
              <div className="rounded bg-slate-elevation5 p-2">
                <WorkflowBlockIcon
                  workflowBlockType={block.block_type}
                  className="size-6"
                />
              </div>

              <div className="flex flex-col gap-1">
                <span className="text-sm">
                  {workflowBlockTitle[block.block_type]}
                </span>
                <span className="flex gap-2 text-xs text-slate-400">
                  {block.label}
                </span>
                {isFinallyBlock && (
                  <span className="w-fit rounded bg-amber-500 px-1.5 py-0.5 text-[10px] font-medium text-black">
                    Execute on any outcome
                  </span>
                )}
              </div>
            </div>
            <div className="flex gap-2">
              {showFailureIndicator && (
                <div className="self-start rounded bg-slate-elevation5 px-2 py-1">
                  <CrossCircledIcon className="size-4 text-destructive" />
                </div>
              )}
              {showSuccessIndicator && (
                <div className="self-start rounded bg-slate-elevation5 px-2 py-1">
                  <CheckCircledIcon className="size-4 text-success" />
                </div>
              )}
              <div className="flex flex-col items-end gap-[1px]">
                <div className="flex gap-1 self-start rounded bg-slate-elevation5 px-2 py-1">
                  {showDiagnosticLink ? (
                    <Link
                      to={`/tasks/${block.task_id}/diagnostics`}
                      onClick={(event) => event.stopPropagation()}
                    >
                      <div className="flex gap-1">
                        <ExternalLinkIcon className="size-4" />
                        <span className="text-xs">Diagnostics</span>
                      </div>
                    </Link>
                  ) : (
                    <>
                      <CubeIcon className="size-4" />
                      <span className="text-xs">Block</span>
                    </>
                  )}
                </div>
                {duration && showDuration && (
                  <div className="pr-[5px] text-xs text-[#00ecff]">
                    {duration}
                  </div>
                )}
              </div>
            </div>
          </div>
          {block.description ? (
            <div className="text-xs text-slate-400">{block.description}</div>
          ) : null}
          {isLoopBlock && (
            <div className="space-y-2 rounded bg-slate-elevation5 px-3 py-2 text-xs">
              <div className="text-slate-300">
                Iterable values:{" "}
                <span className="font-medium text-slate-200">
                  {loopValues.length}
                </span>
              </div>
              {loopValues.length > 0 && (
                <div className="max-h-40 space-y-1 overflow-y-auto pr-1">
                  {loopValues.map((value, index) => (
                    <div key={index} className="text-slate-400">
                      <span className="mr-1 text-slate-500">[{index}]</span>
                      <code className="rounded bg-slate-elevation1 px-1 py-0.5 font-mono text-slate-300">
                        {truncateValue(stringifyTimelineValue(value), 120)}
                      </code>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
          {block.block_type === "conditional" && block.executed_branch_id && (
            <div className="space-y-2 rounded bg-slate-elevation5 px-3 py-2 text-xs">
              {hasEvaluations(block.output) && block.output.evaluations ? (
                // New format: show all branch evaluations
                <div className="space-y-2">
                  {block.output.evaluations.map((evaluation, index) => (
                    <div
                      key={evaluation.branch_id || index}
                      className={cn(
                        "rounded border px-2 py-1.5",
                        evaluation.is_matched
                          ? "border-success/50 bg-success/10"
                          : "border-slate-600 bg-slate-elevation3",
                      )}
                    >
                      {evaluation.is_default ? (
                        <div className="text-slate-300">
                          <span className="font-medium">Default branch</span>
                          {evaluation.is_matched && (
                            <span className="ml-2 text-success">✓ Matched</span>
                          )}
                        </div>
                      ) : (
                        <div className="space-y-1">
                          <div className="text-slate-400">
                            <code className="rounded bg-slate-elevation1 px-1 py-0.5 font-mono text-slate-300">
                              {evaluation.original_expression}
                            </code>
                          </div>
                          {evaluation.rendered_expression &&
                            evaluation.rendered_expression !==
                              evaluation.original_expression && (
                              <div className="text-slate-400">
                                → rendered to{" "}
                                <code className="rounded bg-slate-elevation1 px-1 py-0.5 font-mono text-slate-200">
                                  {evaluation.rendered_expression}
                                </code>
                              </div>
                            )}
                          <div className="flex items-center gap-2">
                            <span className="text-slate-400">evaluated to</span>
                            <span
                              className={cn(
                                "font-medium",
                                evaluation.result
                                  ? "text-success"
                                  : "text-red-400",
                              )}
                            >
                              {evaluation.result ? "True" : "False"}
                            </span>
                            {evaluation.is_matched && (
                              <span className="text-success">✓ Matched</span>
                            )}
                          </div>
                        </div>
                      )}
                      {evaluation.is_matched && evaluation.next_block_label && (
                        <div className="mt-1 text-slate-400">
                          → Executing next block:{" "}
                          <span className="font-medium text-slate-300">
                            {evaluation.next_block_label}
                          </span>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              ) : (
                // Fallback: old format without evaluations array
                <>
                  {block.executed_branch_expression !== null &&
                  block.executed_branch_expression !== undefined ? (
                    <div className="text-slate-300">
                      Condition{" "}
                      <code className="rounded bg-slate-elevation3 px-1.5 py-0.5 font-mono text-slate-200">
                        {block.executed_branch_expression}
                      </code>{" "}
                      evaluated to{" "}
                      <span className="font-medium text-success">True</span>
                    </div>
                  ) : (
                    <div className="text-slate-300">
                      No conditions matched, executing default branch
                    </div>
                  )}
                  {block.executed_branch_next_block && (
                    <div className="text-slate-400">
                      → Executing next block:{" "}
                      <span className="font-medium text-slate-300">
                        {block.executed_branch_next_block}
                      </span>
                    </div>
                  )}
                </>
              )}
            </div>
          )}
        </div>

        {block.block_type === "human_interaction" && (
          <WorkflowRunHumanInteraction workflowRunBlock={block} />
        )}

        {actions.map((action, index) => {
          return (
            <ActionCard
              key={action.action_id}
              action={action}
              active={
                isAction(activeItem) &&
                activeItem.action_id === action.action_id
              }
              index={actions.length - index}
              onClick={(event) => {
                event.stopPropagation();
                const actionItem: ActionItem = {
                  block,
                  action,
                };
                onActionClick(actionItem);
              }}
            />
          );
        })}

        {hasNestedChildren && isLoopBlock && (
          <div className="space-y-2">
            {loopIterationGroups.map((group, groupIndex) => {
              const loopValueFromIterable =
                group.index !== null ? loopValues[group.index] ?? null : null;
              const iterationNumber =
                group.index !== null
                  ? group.index + 1
                  : loopIterationGroups.length - groupIndex;
              const currentValuePreview = truncateValue(
                stringifyTimelineValue(
                  loopValueFromIterable ?? group.currentValue,
                ),
                140,
              );

              return (
                <Collapsible
                  key={`${group.index ?? "unknown"}-${groupIndex}`}
                  open={openLoopGroups.has(groupIndex)}
                  onOpenChange={(open) => {
                    setOpenLoopGroups((prev) => {
                      const next = new Set(prev);
                      if (open) {
                        next.add(groupIndex);
                      } else {
                        next.delete(groupIndex);
                      }
                      return next;
                    });
                  }}
                >
                  <div className="rounded border border-slate-700 bg-slate-elevation4">
                    <CollapsibleTrigger asChild>
                      <button
                        className="group flex w-full items-center justify-between gap-2 px-2 py-1 text-left"
                        onClick={(event) => event.stopPropagation()}
                      >
                        <div className="flex items-center gap-1.5">
                          <ChevronRightIcon className="size-4 text-slate-300 transition-transform group-data-[state=open]:rotate-90" />
                          <span className="text-xs text-slate-200">{`Iteration ${iterationNumber}`}</span>
                        </div>
                        <code className="max-w-[70%] truncate rounded bg-slate-elevation1 px-1 py-0.5 text-[11px] text-slate-300">
                          current_value: {currentValuePreview}
                        </code>
                      </button>
                    </CollapsibleTrigger>
                  </div>
                  <CollapsibleContent className="px-2 pb-2 pt-2">
                    <TimelineSubItems
                      items={group.items}
                      activeItem={activeItem}
                      depth={depth + 1}
                      onActionClick={onActionClick}
                      onBlockItemClick={onBlockItemClick}
                      onThoughtCardClick={onThoughtCardClick}
                      finallyBlockLabel={finallyBlockLabel}
                    />
                  </CollapsibleContent>
                </Collapsible>
              );
            })}
          </div>
        )}

        {hasNestedChildren && isConditionalBlock && (
          <Collapsible open={childrenOpen} onOpenChange={setChildrenOpen}>
            <div className="rounded border border-slate-700 bg-slate-elevation4 px-2 py-1.5">
              <CollapsibleTrigger asChild>
                <button
                  className="flex w-full items-center justify-between gap-2 text-left"
                  onClick={(event) => event.stopPropagation()}
                >
                  <div className="flex items-center gap-1.5 text-xs text-slate-200">
                    {childrenOpen ? (
                      <ChevronDownIcon className="size-4" />
                    ) : (
                      <ChevronRightIcon className="size-4" />
                    )}
                    <span>{`Executed branch blocks (${subItems.length})`}</span>
                  </div>
                  {block.executed_branch_next_block && (
                    <span className="text-[11px] text-slate-400">
                      next: {block.executed_branch_next_block}
                    </span>
                  )}
                </button>
              </CollapsibleTrigger>
            </div>
            <CollapsibleContent className="space-y-2 pt-2">
              <TimelineSubItems
                items={subItems}
                activeItem={activeItem}
                depth={depth + 1}
                onActionClick={onActionClick}
                onBlockItemClick={onBlockItemClick}
                onThoughtCardClick={onThoughtCardClick}
                finallyBlockLabel={finallyBlockLabel}
              />
            </CollapsibleContent>
          </Collapsible>
        )}

        {hasNestedChildren && !isLoopBlock && !isConditionalBlock && (
          <TimelineSubItems
            items={subItems}
            activeItem={activeItem}
            depth={depth + 1}
            onActionClick={onActionClick}
            onBlockItemClick={onBlockItemClick}
            onThoughtCardClick={onThoughtCardClick}
            finallyBlockLabel={finallyBlockLabel}
          />
        )}
      </div>
    </div>
  );
}

export { WorkflowRunTimelineBlockItem };
