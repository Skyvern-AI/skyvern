import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { statusIsFinalized, statusIsNotFinalized } from "@/routes/tasks/types";
import { cn } from "@/util/utils";
import { DotFilledIcon, MagnifyingGlassIcon } from "@radix-ui/react-icons";
import { useEffect, useMemo, useRef, useState } from "react";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { useWorkflowRunTimelineQuery } from "../hooks/useWorkflowRunTimelineQuery";
import {
  countActionsInTimeline,
  countCompletedTopLevelBlocks,
  isBlockItem,
  isObserverThought,
  isThoughtItem,
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
  type SkippedBranchGroup,
  WorkflowRunTimelineBlockItem,
} from "./WorkflowRunTimelineBlockItem";
import { WorkflowRunTimelineUnexecutedBlockItem } from "./WorkflowRunTimelineUnexecutedBlockItem";
import { buildCodeStepsByLabel } from "../workflowBlockUtils";
import {
  classifyUnexecutedDefinedBlocks,
  flattenTimelineChronologically,
  type UnexecutedDefinedBlock,
} from "./workflowTimelineUtils";

type Props = {
  activeItem: WorkflowRunOverviewActiveElement;
  activeIteration?: number | null;
  // When set, read this run's timeline instead of the URL's (studio shell).
  workflowRunId?: string;
  // Studio owns live-status in its own header; let it hide this duplicate badge.
  hideLiveBadge?: boolean;
  // Opt-in label search + jump-to-block; off by default so the legacy run
  // view renders no search UI and stays unchanged.
  enableSearch?: boolean;
  // Studio surfaces the run's elapsed time here, next to the counts, with the
  // created/queued/started/finished breakdown on its hover tooltip. Legacy
  // passes neither and renders no duration.
  elapsed?: string;
  elapsedTitle?: string;
  onLiveStreamSelected: () => void;
  onActionItemSelected: (item: ActionItem) => void;
  onBlockItemSelected: (item: WorkflowRunBlock) => void;
  onThoughtItemSelected: (item: ObserverThought) => void;
  onIterationSelected: (
    loopBlock: WorkflowRunBlock,
    iterationIndex: number,
  ) => void;
};

function buildBlockOrderIndex(
  items: Array<WorkflowRunTimelineItem>,
): ReadonlyMap<string, number> {
  const blocks: Array<{
    id: string;
    createdAt: number;
    sequence: number;
  }> = [];

  function walk(timelineItems: Array<WorkflowRunTimelineItem>) {
    for (const item of timelineItems) {
      if (isBlockItem(item)) {
        const createdAt = new Date(item.created_at).getTime();
        blocks.push({
          id: item.block.workflow_run_block_id,
          createdAt: Number.isNaN(createdAt)
            ? Number.MAX_SAFE_INTEGER
            : createdAt,
          sequence: blocks.length,
        });
      }
      if (item.children.length > 0) {
        walk(item.children);
      }
    }
  }

  walk(items);
  blocks.sort(
    (left, right) =>
      left.createdAt - right.createdAt || left.sequence - right.sequence,
  );

  return new Map(blocks.map((block, index) => [block.id, index + 1]));
}

type TimelineSearchTarget = {
  block: WorkflowRunBlock;
  label: string;
  order: number | null;
};

// Top-level rows only: flattenTimelineChronologically hoists conditional
// branches here, but loop/task_v2 children stay nested with no row to scroll to.
function collectTimelineSearchTargets(
  items: Array<WorkflowRunTimelineItem>,
  blockOrder: ReadonlyMap<string, number>,
): Array<TimelineSearchTarget> {
  const targets: Array<TimelineSearchTarget> = [];
  for (const item of items) {
    if (!isBlockItem(item)) {
      continue;
    }
    const label = item.block.label;
    if (!label || label.trim() === "") {
      continue;
    }
    targets.push({
      block: item.block,
      label,
      order: blockOrder.get(item.block.workflow_run_block_id) ?? null,
    });
  }
  return targets;
}

function filterTimelineSearchTargets(
  targets: Array<TimelineSearchTarget>,
  query: string,
): Array<TimelineSearchTarget> {
  const needle = query.trim().toLowerCase();
  if (needle === "") {
    return targets;
  }
  return targets.filter((target) =>
    target.label.toLowerCase().includes(needle),
  );
}

function TimelineBlockSearch({
  targets,
  onJump,
}: {
  targets: Array<TimelineSearchTarget>;
  onJump: (target: TimelineSearchTarget) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const results = filterTimelineSearchTargets(targets, query);
  const closeAndReset = () => {
    setOpen(false);
    setQuery("");
  };
  return (
    <Popover
      open={open}
      onOpenChange={(next) => (next ? setOpen(true) : closeAndReset())}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label="Search blocks"
          className="ml-auto inline-flex shrink-0 cursor-pointer items-center rounded p-1 text-slate-400 transition-colors hover:bg-slate-700 hover:text-slate-200"
        >
          <MagnifyingGlassIcon className="size-3.5" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" sideOffset={6} className="w-64 p-0">
        <Command
          shouldFilter={false}
          onKeyDown={(event) => {
            // Keep Escape local: Studio may mount the editor canvas beside the
            // run view, whose window Escape handler would clear its selection.
            if (event.key === "Escape") {
              event.stopPropagation();
              closeAndReset();
            }
          }}
        >
          <CommandInput
            placeholder="Search blocks…"
            value={query}
            onValueChange={setQuery}
          />
          <CommandList>
            <CommandEmpty>No blocks found.</CommandEmpty>
            {results.length > 0 ? (
              <CommandGroup>
                {results.map((target) => (
                  <CommandItem
                    key={target.block.workflow_run_block_id}
                    value={target.block.workflow_run_block_id}
                    onSelect={() => {
                      onJump(target);
                      closeAndReset();
                    }}
                  >
                    {target.order !== null ? (
                      <span className="mr-2 shrink-0 text-muted-foreground">
                        #{target.order}
                      </span>
                    ) : null}
                    <span className="truncate">{target.label}</span>
                  </CommandItem>
                ))}
              </CommandGroup>
            ) : null}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

function WorkflowRunTimeline({
  activeItem,
  activeIteration = null,
  workflowRunId,
  hideLiveBadge = false,
  enableSearch = false,
  elapsed,
  elapsedTitle,
  onLiveStreamSelected,
  onActionItemSelected,
  onBlockItemSelected,
  onThoughtItemSelected,
  onIterationSelected,
}: Props) {
  const { data: workflowRun, isLoading: workflowRunIsLoading } =
    useWorkflowRunWithWorkflowQuery({ workflowRunId });

  const { data: workflowRunTimeline, isLoading: workflowRunTimelineIsLoading } =
    useWorkflowRunTimelineQuery({ workflowRunId });
  const displayTimeline = useMemo(
    () => flattenTimelineChronologically(workflowRunTimeline ?? []),
    [workflowRunTimeline],
  );
  const blockOrder = useMemo(
    () => buildBlockOrderIndex(workflowRunTimeline ?? []),
    [workflowRunTimeline],
  );
  const searchTargets = useMemo(
    () =>
      enableSearch
        ? collectTimelineSearchTargets(displayTimeline, blockOrder)
        : [],
    [enableSearch, displayTimeline, blockOrder],
  );
  const blockElementsRef = useRef<Map<string, HTMLDivElement>>(new Map());
  const registerBlockElement = (id: string, el: HTMLDivElement | null) => {
    if (el) {
      blockElementsRef.current.set(id, el);
    } else {
      blockElementsRef.current.delete(id);
    }
  };
  const jumpToBlock = (target: TimelineSearchTarget) => {
    onBlockItemSelected(target.block);
    const el = blockElementsRef.current.get(target.block.workflow_run_block_id);
    if (!el) {
      return;
    }
    const reduceMotion =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    el.scrollIntoView({
      behavior: reduceMotion ? "auto" : "smooth",
      block: "start",
    });
  };
  const codeStepsByLabel = useMemo(
    () =>
      buildCodeStepsByLabel(
        workflowRun?.workflow?.workflow_definition?.blocks ?? [],
      ),
    [workflowRun],
  );
  const workflowRunIsNotFinalized = workflowRun
    ? statusIsNotFinalized(workflowRun)
    : false;
  const workflowRunIsFinalized = workflowRun
    ? statusIsFinalized(workflowRun)
    : false;
  const definedBlocks = useMemo(
    () => workflowRun?.workflow?.workflow_definition?.blocks ?? [],
    [workflowRun?.workflow?.workflow_definition?.blocks],
  );
  const unexecutedBlocks = useMemo(
    () =>
      workflowRunIsFinalized
        ? classifyUnexecutedDefinedBlocks(
            definedBlocks,
            workflowRunTimeline ?? [],
          )
        : [],
    [definedBlocks, workflowRunIsFinalized, workflowRunTimeline],
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

  if (workflowRunIsLoading || workflowRunTimelineIsLoading) {
    return <Skeleton className="h-full w-full" />;
  }

  if (!workflowRun || !workflowRunTimeline) {
    return null;
  }

  const finallyBlockLabel =
    workflowRun.workflow?.workflow_definition?.finally_block_label ?? null;

  const numberOfActions = countActionsInTimeline(workflowRunTimeline);
  const totalBlocks = definedBlocks.length;
  const completedBlocks = countCompletedTopLevelBlocks(workflowRunTimeline);

  return (
    <div className="flex h-full min-w-0 flex-col overflow-hidden rounded-md border border-border bg-slate-elevation1">
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2 text-xs">
        <span className="font-medium text-foreground dark:text-slate-200">
          Timeline
        </span>
        {elapsed ? (
          <span
            className="text-muted-foreground dark:text-slate-500"
            title={elapsedTitle || undefined}
          >
            · {elapsed}
          </span>
        ) : null}
        {totalBlocks > 0 && (
          <span
            className="text-muted-foreground dark:text-slate-500"
            title="Top-level blocks completed out of the total defined for this workflow"
          >
            · {completedBlocks}/{totalBlocks} blocks
          </span>
        )}
        {numberOfActions > 0 && (
          <span className="text-muted-foreground dark:text-slate-500">
            · {numberOfActions} {numberOfActions === 1 ? "action" : "actions"}
          </span>
        )}
        <span className="text-muted-foreground dark:text-slate-500">
          · {workflowRun.total_steps ?? 0}{" "}
          {(workflowRun.total_steps ?? 0) === 1 ? "step" : "steps"}
        </span>
        <span
          className="text-muted-foreground dark:text-slate-500"
          title="Credits consumed by this run (live + cached)"
        >
          ·{" "}
          {(
            (workflowRun.credits_used ?? 0) +
            (workflowRun.cached_credits_used ?? 0)
          ).toLocaleString()}{" "}
          credits
        </span>
        {workflowRunIsNotFinalized && !hideLiveBadge && (
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
        {enableSearch && (
          <TimelineBlockSearch targets={searchTargets} onJump={jumpToBlock} />
        )}
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <ScrollAreaViewport className="h-full max-h-full [&>div]:!block [&>div]:!overflow-x-hidden">
          <div className="p-2">
            {workflowRunIsNotFinalized && workflowRunTimeline.length === 0 && (
              <div className="flex items-center justify-center py-8 text-sm text-muted-foreground">
                Formulating actions...
              </div>
            )}
            {workflowRunIsFinalized && workflowRunTimeline.length === 0 && (
              <div>Workflow timeline is empty</div>
            )}
            {displayTimeline.map((timelineItem) => {
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
                    ref={
                      enableSearch
                        ? (el) =>
                            registerBlockElement(
                              timelineItem.block.workflow_run_block_id,
                              el,
                            )
                        : undefined
                    }
                    className={cn({
                      "duration-300 animate-in fade-in slide-in-from-top-3":
                        isNew,
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
                      isNew &&
                        "duration-300 animate-in fade-in slide-in-from-top-3",
                    )}
                  >
                    <ThoughtCard
                      active={
                        isObserverThought(activeItem) &&
                        activeItem.thought_id ===
                          timelineItem.thought.thought_id
                      }
                      onClick={onThoughtItemSelected}
                      thought={timelineItem.thought}
                    />
                  </div>
                );
              }
              return null;
            })}
            {trailingUnexecutedBlocks.map(({ block, reason }) => (
              <WorkflowRunTimelineUnexecutedBlockItem
                key={`unexecuted-${block.label}`}
                block={block}
                reason={reason}
              />
            ))}
          </div>
        </ScrollAreaViewport>
      </ScrollArea>
    </div>
  );
}

export { WorkflowRunTimeline };
