import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { statusIsFinalized, statusIsNotFinalized } from "@/routes/tasks/types";
import { cn } from "@/util/utils";
import { DotFilledIcon } from "@radix-ui/react-icons";
import { useEffect, useRef } from "react";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { useWorkflowRunTimelineQuery } from "../hooks/useWorkflowRunTimelineQuery";
import {
  countActionsInTimeline,
  isBlockItem,
  isObserverThought,
  isThoughtItem,
  ObserverThought,
  WorkflowRunBlock,
} from "../types/workflowRunTypes";
import { ThoughtCard } from "./ThoughtCard";
import {
  ActionItem,
  WorkflowRunOverviewActiveElement,
} from "./WorkflowRunOverview";
import { WorkflowRunTimelineBlockItem } from "./WorkflowRunTimelineBlockItem";

type Props = {
  activeItem: WorkflowRunOverviewActiveElement;
  onLiveStreamSelected: () => void;
  onObserverThoughtCardSelected: (item: ObserverThought) => void;
  onActionItemSelected: (item: ActionItem) => void;
  onBlockItemSelected: (item: WorkflowRunBlock) => void;
};

function WorkflowRunTimeline({
  activeItem,
  onLiveStreamSelected,
  onObserverThoughtCardSelected,
  onActionItemSelected,
  onBlockItemSelected,
}: Props) {
  const { data: workflowRun, isLoading: workflowRunIsLoading } =
    useWorkflowRunWithWorkflowQuery();

  const { data: workflowRunTimeline, isLoading: workflowRunTimelineIsLoading } =
    useWorkflowRunTimelineQuery();

  // Track known item IDs so we can animate only newly-arrived items
  const knownItemIdsRef = useRef<Set<string>>(new Set());
  const isInitialRenderRef = useRef(true);

  // After each render, sync the known set and clear the initial-render flag.
  // Important: the isNew check in the JSX below runs during render (before
  // this effect), so it correctly compares against the previous render's set.
  useEffect(() => {
    if (!workflowRunTimeline) return;
    const ids = new Set<string>();
    for (const item of workflowRunTimeline) {
      if (isBlockItem(item)) {
        ids.add(item.block.workflow_run_block_id);
      } else if (isThoughtItem(item)) {
        ids.add(item.thought.thought_id);
      }
    }
    knownItemIdsRef.current = ids;
    isInitialRenderRef.current = false;
  }, [workflowRunTimeline]);

  if (workflowRunIsLoading || workflowRunTimelineIsLoading) {
    return <Skeleton className="h-full w-full" />;
  }

  if (!workflowRun || !workflowRunTimeline) {
    return null;
  }

  // bit redundant but better read
  const workflowRunIsNotFinalized = statusIsNotFinalized(workflowRun);
  const workflowRunIsFinalized = statusIsFinalized(workflowRun);

  const finallyBlockLabel =
    workflowRun.workflow?.workflow_definition?.finally_block_label ?? null;

  const numberOfActions = countActionsInTimeline(workflowRunTimeline);

  return (
    <div className="min-w-0 space-y-4 overflow-hidden rounded bg-slate-elevation1 p-4">
      <div className="grid grid-cols-2 gap-2">
        <div className="flex items-center justify-center rounded bg-slate-elevation3 px-4 py-3 text-xs">
          Actions: {numberOfActions}
        </div>
        <div className="flex items-center justify-center rounded bg-slate-elevation3 px-4 py-3 text-xs">
          Steps: {workflowRun.total_steps ?? 0}
        </div>
      </div>
      <ScrollArea>
        <ScrollAreaViewport className="h-[37rem] max-h-[37rem] [&>div]:!block [&>div]:!overflow-x-hidden">
          <div className="space-y-4 p-1">
            {workflowRunIsNotFinalized && (
              <div
                key="stream"
                className={cn(
                  "flex cursor-pointer items-center gap-2 rounded-lg bg-gradient-to-r from-red-500/10 to-slate-elevation3 px-3 py-2 text-sm transition-colors duration-150 hover:from-red-500/20",
                  {
                    "bg-slate-elevation5 from-red-500/20":
                      activeItem === "stream",
                  },
                )}
                onClick={onLiveStreamSelected}
              >
                <DotFilledIcon className="h-5 w-5 animate-pulse text-destructive" />
                Live
              </div>
            )}
            {workflowRunIsNotFinalized && workflowRunTimeline.length === 0 && (
              <div className="flex items-center justify-center py-8 text-sm text-slate-400">
                Formulating actions...
              </div>
            )}
            {workflowRunIsFinalized && workflowRunTimeline.length === 0 && (
              <div>Workflow timeline is empty</div>
            )}
            {workflowRunTimeline?.map((timelineItem) => {
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
                      "duration-300 animate-in fade-in slide-in-from-top-3":
                        isNew,
                    })}
                  >
                    <WorkflowRunTimelineBlockItem
                      subItems={timelineItem.children}
                      activeItem={activeItem}
                      block={timelineItem.block}
                      onActionClick={onActionItemSelected}
                      onBlockItemClick={onBlockItemSelected}
                      onThoughtCardClick={onObserverThoughtCardSelected}
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
                    className={cn({
                      "duration-300 animate-in fade-in slide-in-from-top-3":
                        isNew,
                    })}
                  >
                    <ThoughtCard
                      active={
                        isObserverThought(activeItem) &&
                        activeItem.thought_id ===
                          timelineItem.thought.thought_id
                      }
                      onClick={onObserverThoughtCardSelected}
                      thought={timelineItem.thought}
                    />
                  </div>
                );
              }
            })}
          </div>
        </ScrollAreaViewport>
      </ScrollArea>
    </div>
  );
}

export { WorkflowRunTimeline };
