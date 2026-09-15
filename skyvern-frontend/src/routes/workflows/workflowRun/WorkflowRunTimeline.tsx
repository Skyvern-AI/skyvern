import { formatCodeBlockErrorCode } from "./codeBlockFailure";
import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { type WorkflowRunStatusApiResponseWithWorkflow } from "@/api/types";
import {
  getRunAttempt,
  runIsExecuting,
  runIsLogicallyFinal,
  runIsRetryWaiting,
} from "./runRetryState";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { formatElapsed } from "../studio/runProjections";
import { cn } from "@/util/utils";
import { DotFilledIcon } from "@radix-ui/react-icons";
import { useEffect, useMemo, useRef, useState } from "react";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { useWorkflowRunTimelineQuery } from "../hooks/useWorkflowRunTimelineQuery";
import {
  countActionsInTimeline,
  countCompletedTopLevelBlocks,
  isAction,
  isBlockItem,
  isObserverThought,
  isThoughtItem,
  isWorkflowRunBlock,
  ObserverThought,
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";
import {
  ActionItem,
  WorkflowRunOverviewActiveElement,
} from "./WorkflowRunOverview";
import { ThoughtCard } from "./ThoughtCard";
import {
  StatusDot,
  type SkippedBranchGroup,
  WorkflowRunTimelineBlockItem,
} from "./WorkflowRunTimelineBlockItem";
import { WorkflowRunTimelineUnexecutedBlockItem } from "./WorkflowRunTimelineUnexecutedBlockItem";
import { buildCodeStepsByLabel } from "../workflowBlockUtils";
import {
  groupTimelineByAttempt,
  filterTimelineToAttempt,
  findActiveItem,
  buildBlockOrderIndex,
  classifyUnexecutedDefinedBlocks,
  flattenTimelineChronologically,
  type UnexecutedDefinedBlock,
} from "./workflowTimelineUtils";

type Props = {
  activeItem: WorkflowRunOverviewActiveElement;
  activeIteration?: number | null;
  // The run to read. Stated by every caller so the queries never fall back to a
  // route id the caller is not looking at.
  workflowRunId: string | undefined;
  // In the studio the pane already paints this exact surface, so the card would
  // be a box drawn inside its own fill. Legacy sits in a sidebar column on the
  // page background, where the border does separate — so it keeps it.
  hideBorder?: boolean;
  // Studio composes its own header (RunSummaryStrip: status, duration, counts,
  // search) above this list, so neither the label row nor the counts row
  // renders. Legacy keeps both — they are that page's only timeline title bar.
  hideHeader?: boolean;
  onLiveStreamSelected: () => void;
  onActionItemSelected: (item: ActionItem) => void;
  onBlockItemSelected: (item: WorkflowRunBlock) => void;
  onThoughtItemSelected: (item: ObserverThought) => void;
  onIterationSelected: (
    loopBlock: WorkflowRunBlock,
    iterationIndex: number,
  ) => void;
};

/**
 * The run's executed-block, credit, and action counts. Rendered by this
 * component's legacy header row and by the studio's RunSummaryStrip, so the
 * two surfaces never drift.
 */
function TimelineRunCounts({
  workflowRun,
  timeline,
}: {
  workflowRun: WorkflowRunStatusApiResponseWithWorkflow;
  timeline: Array<WorkflowRunTimelineItem>;
}) {
  const numberOfActions = countActionsInTimeline(timeline);
  const numberOfBlocks = timeline.filter(isBlockItem).length;
  const totalBlocks =
    workflowRun.workflow?.workflow_definition?.blocks?.length ?? 0;
  const completedBlocks = countCompletedTopLevelBlocks(timeline);
  return (
    <>
      {totalBlocks > 0 ? (
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <span
                className="cursor-default whitespace-nowrap text-muted-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring dark:text-slate-500"
                tabIndex={0}
              >
                <span className="tabular-nums text-foreground dark:text-slate-300">
                  {numberOfBlocks}
                </span>{" "}
                {numberOfBlocks === 1 ? "block" : "blocks"}
              </span>
            </TooltipTrigger>
            <TooltipContent side="bottom" className="text-left">
              {completedBlocks}/{totalBlocks} blocks
              <span className="block">
                Top-level blocks completed out of the total defined for this
                workflow
              </span>
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
      ) : (
        // No configured blocks → no ratio to explain; plain text, not an
        // empty focus stop.
        <span className="whitespace-nowrap text-muted-foreground dark:text-slate-500">
          <span className="tabular-nums text-foreground dark:text-slate-300">
            {numberOfBlocks}
          </span>{" "}
          {numberOfBlocks === 1 ? "block" : "blocks"}
        </span>
      )}
      <span
        className="whitespace-nowrap text-muted-foreground dark:text-slate-500"
        title="Credits consumed by this run (live + cached)"
      >
        <span className="tabular-nums text-foreground dark:text-slate-300">
          {(
            (workflowRun.credits_used ?? 0) +
            (workflowRun.cached_credits_used ?? 0)
          ).toLocaleString()}
        </span>{" "}
        credits
      </span>
      {numberOfActions > 0 ? (
        <span className="whitespace-nowrap text-muted-foreground dark:text-slate-500">
          <span className="tabular-nums text-foreground dark:text-slate-300">
            {numberOfActions}
          </span>{" "}
          {numberOfActions === 1 ? "action" : "actions"}
        </span>
      ) : null}
    </>
  );
}

function WorkflowRunTimeline({
  activeItem,
  activeIteration = null,
  workflowRunId,
  hideBorder = false,
  hideHeader = false,
  onLiveStreamSelected,
  onActionItemSelected,
  onBlockItemSelected,
  onThoughtItemSelected,
  onIterationSelected,
}: Props) {
  const {
    data: workflowRun,
    isLoading: workflowRunIsLoading,
    isPlaceholderData: runIsWithheld,
    isError: statusUnavailable,
  } = useWorkflowRunWithWorkflowQuery({ workflowRunId });

  const { data: workflowRunTimeline, isLoading: workflowRunTimelineIsLoading } =
    useWorkflowRunTimelineQuery({ workflowRunId });
  const currentAttempt = getRunAttempt(workflowRun ?? {});
  const groups = useMemo(
    () =>
      groupTimelineByAttempt(
        workflowRunTimeline ?? [],
        workflowRun?.attempts ?? [],
        currentAttempt,
      ).map((group) => ({
        ...group,
        blockOrder: buildBlockOrderIndex(group.items),
      })),
    [workflowRunTimeline, workflowRun?.attempts, currentAttempt],
  );
  const currentTimeline = useMemo(
    () =>
      filterTimelineToAttempt(
        workflowRunTimeline ?? [],
        workflowRun?.attempts ?? [],
        currentAttempt,
      ),
    [workflowRunTimeline, workflowRun?.attempts, currentAttempt],
  );
  const activeItemId = isWorkflowRunBlock(activeItem)
    ? activeItem.workflow_run_block_id
    : isObserverThought(activeItem)
      ? activeItem.thought_id
      : isAction(activeItem)
        ? activeItem.action_id
        : null;
  const activeAttempt = useMemo(
    () =>
      activeItemId === null
        ? undefined
        : groups.find((group) =>
            findActiveItem(group.items, activeItemId, false),
          )?.attemptNumber,
    [activeItemId, groups],
  );
  const [expansion, setExpansion] = useState(() => ({
    workflowRunId,
    attempt: currentAttempt,
    activeItemId,
    activeAttempt,
    overrides: new Map<number, boolean>(),
  }));
  if (
    expansion.workflowRunId !== workflowRunId ||
    expansion.attempt !== currentAttempt ||
    expansion.activeItemId !== activeItemId ||
    expansion.activeAttempt !== activeAttempt
  ) {
    const overrides =
      expansion.workflowRunId === workflowRunId
        ? new Map(expansion.overrides)
        : new Map<number, boolean>();
    if (expansion.attempt !== currentAttempt) {
      overrides.delete(currentAttempt);
    }
    if (activeAttempt !== undefined) {
      overrides.delete(activeAttempt);
    }
    setExpansion({
      workflowRunId,
      attempt: currentAttempt,
      activeItemId,
      activeAttempt,
      overrides,
    });
  }
  const displayTimeline = useMemo(
    () => flattenTimelineChronologically(workflowRunTimeline ?? []),
    [workflowRunTimeline],
  );
  const codeStepsByLabel = useMemo(
    () =>
      buildCodeStepsByLabel(
        workflowRun?.workflow?.workflow_definition?.blocks ?? [],
      ),
    [workflowRun],
  );
  const workflowRunIsExecuting =
    workflowRun && !statusUnavailable ? runIsExecuting(workflowRun) : false;
  const workflowRunIsFinalized = workflowRun
    ? runIsLogicallyFinal(workflowRun)
    : false;
  const definedBlocks = useMemo(
    () => workflowRun?.workflow?.workflow_definition?.blocks ?? [],
    [workflowRun?.workflow?.workflow_definition?.blocks],
  );
  const unexecutedBlocks = useMemo(
    () =>
      workflowRunIsFinalized
        ? classifyUnexecutedDefinedBlocks(definedBlocks, currentTimeline)
        : [],
    [definedBlocks, workflowRunIsFinalized, currentTimeline],
  );
  const { skippedBranchBlocksByConditionalId, trailingUnexecutedBlocks } =
    useMemo(() => {
      const skippedBranchGroupsByConditionalId = new Map<
        string,
        Array<SkippedBranchGroup>
      >();
      const trailingBlocks: Array<UnexecutedDefinedBlock> = [];

      unexecutedBlocks.forEach((item) => {
        if (
          item.reason === "branch_not_taken" &&
          item.skippedByWorkflowRunBlockId
        ) {
          const skippedBranchGroups =
            skippedBranchGroupsByConditionalId.get(
              item.skippedByWorkflowRunBlockId,
            ) ?? [];
          const branchKey =
            item.skippedBranch?.key ?? item.skippedBranch?.nextBlockLabel;
          const skippedBranch = item.skippedBranch;
          if (!branchKey || !skippedBranch) {
            trailingBlocks.push(item);
            return;
          }
          const skippedBranchGroup = skippedBranchGroups.find(
            (group) => group.key === branchKey,
          );
          if (skippedBranchGroup) {
            skippedBranchGroup.blocks.push(item);
          } else {
            skippedBranchGroups.push({
              key: branchKey,
              branch: skippedBranch,
              blocks: [item],
            });
          }
          skippedBranchGroupsByConditionalId.set(
            item.skippedByWorkflowRunBlockId,
            skippedBranchGroups,
          );
          return;
        }
        trailingBlocks.push(item);
      });

      return {
        skippedBranchBlocksByConditionalId: skippedBranchGroupsByConditionalId,
        trailingUnexecutedBlocks: trailingBlocks,
      };
    }, [unexecutedBlocks]);

  // Track known item IDs so we can animate only newly-arrived items
  const knownItemIdsRef = useRef<Set<string>>(new Set());
  const isInitialRenderRef = useRef(true);

  // After each render, sync the known set and clear the initial-render flag.
  // Important: the isNew check in the JSX below runs during render (before
  // this effect), so it correctly compares against the previous render's set.
  useEffect(() => {
    if (!workflowRunTimeline) return;
    const ids = new Set<string>();
    for (const item of displayTimeline) {
      if (isBlockItem(item)) {
        ids.add(item.block.workflow_run_block_id);
      } else if (isThoughtItem(item)) {
        ids.add(item.thought.thought_id);
      }
    }
    knownItemIdsRef.current = ids;
    isInitialRenderRef.current = false;
  }, [displayTimeline, workflowRunTimeline]);

  if (workflowRunIsLoading || workflowRunTimelineIsLoading || runIsWithheld) {
    return <Skeleton className="h-full w-full" />;
  }

  if (!workflowRun || !workflowRunTimeline) {
    return null;
  }

  const finallyBlockLabel =
    workflowRun.workflow?.workflow_definition?.finally_block_label ?? null;

  const showAttemptGroups = groups.length > 1 || currentAttempt > 1;
  const unexecutedRows = trailingUnexecutedBlocks.map(({ block, reason }) => (
    <WorkflowRunTimelineUnexecutedBlockItem
      key={`unexecuted-${block.label}`}
      block={block}
      reason={reason}
    />
  ));
  const renderTimelineItem = (
    timelineItem: WorkflowRunTimelineItem,
    blockOrder: ReadonlyMap<string, number> | undefined,
  ) => {
    const itemId = isBlockItem(timelineItem)
      ? timelineItem.block.workflow_run_block_id
      : isThoughtItem(timelineItem)
        ? timelineItem.thought.thought_id
        : null;
    const isNew =
      itemId !== null &&
      !isInitialRenderRef.current &&
      !knownItemIdsRef.current.has(itemId);

    if (isBlockItem(timelineItem)) {
      return (
        <div
          key={timelineItem.block.workflow_run_block_id}
          className={cn({
            "duration-300 animate-in fade-in slide-in-from-top-3": isNew,
          })}
        >
          <WorkflowRunTimelineBlockItem
            subItems={timelineItem.children}
            activeItem={activeItem}
            activeIteration={activeIteration}
            block={timelineItem.block}
            blockOrder={blockOrder}
            codeStepsByLabel={codeStepsByLabel}
            skippedBranchBlocksByConditionalId={
              skippedBranchBlocksByConditionalId
            }
            onActionClick={onActionItemSelected}
            onBlockItemClick={onBlockItemSelected}
            onIterationClick={onIterationSelected}
            onThoughtClick={onThoughtItemSelected}
            finallyBlockLabel={finallyBlockLabel}
            workflowRunIsFinalized={workflowRunIsFinalized}
          />
        </div>
      );
    }
    if (isThoughtItem(timelineItem)) {
      return (
        <div
          key={timelineItem.thought.thought_id}
          className={cn(
            "py-1",
            isNew && "duration-300 animate-in fade-in slide-in-from-top-3",
          )}
        >
          <ThoughtCard
            active={
              isObserverThought(activeItem) &&
              activeItem.thought_id === timelineItem.thought.thought_id
            }
            onClick={onThoughtItemSelected}
            thought={timelineItem.thought}
          />
        </div>
      );
    }
    return null;
  };

  return (
    <div
      className={cn(
        "flex h-full min-w-0 flex-col overflow-hidden",
        !hideBorder && "rounded-md border border-border bg-slate-elevation1",
      )}
    >
      {hideHeader ? null : (
        <>
          <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2 text-xs">
            <span className="shrink-0 font-medium text-foreground dark:text-slate-200">
              Timeline
            </span>
            <div className="min-w-0 flex-1" />
            {workflowRunIsExecuting && (
              <button
                type="button"
                onClick={onLiveStreamSelected}
                aria-pressed={activeItem === "stream"}
                aria-label="Jump to the live stream of the running workflow"
                className={cn(
                  "ml-auto inline-flex shrink-0 cursor-pointer items-center gap-1 rounded bg-destructive/15 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-destructive ring-1 ring-transparent transition-all hover:bg-destructive/25",
                  activeItem === "stream" &&
                    "bg-destructive/25 ring-destructive/40",
                )}
              >
                <DotFilledIcon className="size-3 animate-pulse" />
                <span>Live</span>
              </button>
            )}
          </div>
          {/* The run's counts sit on their own full-width row rather than in the
          header, so they are free to reflow to a second line. Crammed into the
          fixed-height header they broke *inside* each value in a narrow pane —
          "· 0/2 / blocks" — and the last one still clipped off the edge. */}
          <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-3 py-1.5 text-xs">
            <TimelineRunCounts
              workflowRun={workflowRun}
              timeline={currentTimeline}
            />
          </div>
        </>
      )}
      <ScrollArea className="min-h-0 flex-1">
        <ScrollAreaViewport className="h-full max-h-full [&>div]:!block [&>div]:!overflow-x-hidden">
          <div className="p-2">
            {!showAttemptGroups &&
              workflowRunIsExecuting &&
              workflowRunTimeline.length === 0 && (
                <div className="flex items-center justify-center py-8 text-sm text-muted-foreground">
                  Formulating actions...
                </div>
              )}
            {!showAttemptGroups &&
              workflowRunIsFinalized &&
              workflowRunTimeline.length === 0 && (
                <div>Workflow timeline is empty</div>
              )}
            {showAttemptGroups
              ? groups.map((group) => {
                  const status =
                    group.summary?.status ??
                    (group.isCurrent ? workflowRun.status : null);
                  return (
                    <Collapsible
                      key={group.attemptNumber}
                      open={
                        expansion.overrides.get(group.attemptNumber) ??
                        (group.isCurrent ||
                          group.attemptNumber === activeAttempt)
                      }
                      onOpenChange={(open) =>
                        setExpansion((previous) => {
                          const overrides = new Map(previous.overrides);
                          overrides.set(group.attemptNumber, open);
                          return { ...previous, overrides };
                        })
                      }
                    >
                      <CollapsibleTrigger className="flex w-full items-center gap-2 rounded px-2 py-2 text-left text-xs hover:bg-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring">
                        <StatusDot
                          status={status}
                          isFinalized={
                            !group.isCurrent || workflowRunIsFinalized
                          }
                        />
                        <span className="font-medium">
                          Attempt {group.attemptNumber}
                        </span>
                        {status ? (
                          <span>{status.replace("_", " ")}</span>
                        ) : null}
                        {group.isCurrent && runIsRetryWaiting(workflowRun) ? (
                          <span>Retry pending</span>
                        ) : null}
                        {group.summary?.error_codes[0] ? (
                          <span>
                            {formatCodeBlockErrorCode(
                              group.summary.error_codes[0],
                            )}
                          </span>
                        ) : null}
                        {group.summary?.started_at ? (
                          <span className="ml-auto tabular-nums text-muted-foreground">
                            {formatElapsed(
                              group.summary.started_at,
                              group.summary.finished_at,
                            )}
                          </span>
                        ) : null}
                      </CollapsibleTrigger>
                      <CollapsibleContent>
                        {group.items.length === 0 &&
                          (group.isCurrent && workflowRunIsExecuting ? (
                            <div className="flex items-center justify-center py-8 text-sm text-muted-foreground">
                              Formulating actions...
                            </div>
                          ) : (
                            <div>Workflow timeline is empty</div>
                          ))}
                        {flattenTimelineChronologically(group.items).map(
                          (item) => renderTimelineItem(item, group.blockOrder),
                        )}
                        {group.isCurrent ? unexecutedRows : null}
                      </CollapsibleContent>
                    </Collapsible>
                  );
                })
              : displayTimeline.map((item) =>
                  renderTimelineItem(item, groups[0]?.blockOrder),
                )}
            {!showAttemptGroups ? unexecutedRows : null}
          </div>
        </ScrollAreaViewport>
      </ScrollArea>
    </div>
  );
}

export { TimelineRunCounts, WorkflowRunTimeline };
