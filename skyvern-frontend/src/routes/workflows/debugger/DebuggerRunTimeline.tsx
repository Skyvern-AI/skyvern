import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { statusIsFinalized } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "../hooks/useWorkflowRunQuery";
import { useWorkflowRunTimelineQuery } from "../hooks/useWorkflowRunTimelineQuery";
import {
  isBlockItem,
  isObserverThought,
  isTaskVariantBlockItem,
  isThoughtItem,
  ObserverThought,
  WorkflowRunBlock,
} from "../types/workflowRunTypes";
import { ThoughtCard } from "@/routes/workflows/workflowRun/ThoughtCard";
import {
  ActionItem,
  WorkflowRunOverviewActiveElement,
} from "@/routes/workflows/workflowRun/WorkflowRunOverview";
import { WorkflowRunTimelineBlockItem } from "@/routes/workflows/workflowRun/WorkflowRunTimelineBlockItem";
import { cn } from "@/util/utils";

type Props = {
  activeItem: WorkflowRunOverviewActiveElement;
  onObserverThoughtCardSelected: (item: ObserverThought) => void;
  onActionItemSelected: (item: ActionItem) => void;
  onBlockItemSelected: (item: WorkflowRunBlock) => void;
};

function DebuggerRunTimeline({
  activeItem,
  onObserverThoughtCardSelected,
  onActionItemSelected,
  onBlockItemSelected,
}: Props) {
  const { data: workflowRun, isLoading: workflowRunIsLoading } =
    useWorkflowRunQuery();

  const { data: workflowRunTimeline, isLoading: workflowRunTimelineIsLoading } =
    useWorkflowRunTimelineQuery();

  if (workflowRunIsLoading || workflowRunTimelineIsLoading) {
    return <Skeleton className="h-full w-full" />;
  }

  if (!workflowRun || !workflowRunTimeline) {
    return null;
  }

  const workflowRunIsFinalized = statusIsFinalized(workflowRun);

  const numberOfActions = workflowRunTimeline.reduce((total, current) => {
    if (isTaskVariantBlockItem(current)) {
      return total + current.block!.actions!.length;
    }
    return total + 0;
  }, 0);

  const firstActionOrThoughtIsPending =
    !workflowRunIsFinalized && workflowRunTimeline.length === 0;

  return (
    <div
      className={cn("w-full min-w-0 space-y-4 rounded p-4", {
        "animate-pulse": firstActionOrThoughtIsPending,
      })}
    >
      <div className="grid w-full grid-cols-2 gap-2">
        <div className="flex items-center justify-center rounded bg-slate-elevation3 px-4 py-3 text-xs">
          Actions: {numberOfActions}
        </div>
        <div className="flex items-center justify-center rounded bg-slate-elevation3 px-4 py-3 text-xs">
          Steps: {workflowRun.total_steps ?? 0}
        </div>
      </div>
      <ScrollArea>
        <ScrollAreaViewport className="h-full w-full">
          <div className="w-full space-y-4">
            {workflowRunIsFinalized && workflowRunTimeline.length === 0 && (
              <div>Workflow timeline is empty</div>
            )}
            {!workflowRunIsFinalized && workflowRunTimeline.length === 0 && (
              <div className="flex h-full w-full items-center justify-center">
                Formulating actions...
              </div>
            )}
            {workflowRunTimeline?.map((timelineItem, i) => {
              if (isBlockItem(timelineItem)) {
                return (
                  <div
                    className={cn({
                      "animate-pulse": !workflowRunIsFinalized && i === 0,
                    })}
                    key={timelineItem.block.workflow_run_block_id}
                  >
                    <WorkflowRunTimelineBlockItem
                      subItems={timelineItem.children}
                      activeItem={activeItem}
                      block={timelineItem.block}
                      onActionClick={onActionItemSelected}
                      onBlockItemClick={onBlockItemSelected}
                      onThoughtCardClick={onObserverThoughtCardSelected}
                    />
                  </div>
                );
              }
              if (isThoughtItem(timelineItem)) {
                return (
                  <div
                    className={cn({
                      "animate-pulse": !workflowRunIsFinalized && i === 0,
                    })}
                    key={timelineItem.thought.thought_id}
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

export { DebuggerRunTimeline };
