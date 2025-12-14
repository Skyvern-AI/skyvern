import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { statusIsFinalized, statusIsNotFinalized } from "@/routes/tasks/types";
import { cn } from "@/util/utils";
import { DotFilledIcon } from "@radix-ui/react-icons";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { useWorkflowRunTimelineQuery } from "../hooks/useWorkflowRunTimelineQuery";
import {
  isBlockItem,
  isObserverThought,
  isTaskVariantBlockItem,
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

  if (workflowRunIsLoading || workflowRunTimelineIsLoading) {
    return <Skeleton className="h-full w-full" />;
  }

  if (!workflowRun || !workflowRunTimeline) {
    return null;
  }

  // bit redundant but better read
  const workflowRunIsNotFinalized = statusIsNotFinalized(workflowRun);
  const workflowRunIsFinalized = statusIsFinalized(workflowRun);

  const numberOfActions = workflowRunTimeline.reduce((total, current) => {
    if (isTaskVariantBlockItem(current)) {
      return total + current.block!.actions!.length;
    }
    return total + 0;
  }, 0);

  return (
    <div className="min-w-0 space-y-4 rounded bg-slate-elevation1 p-4">
      <div className="grid grid-cols-2 gap-2">
        <div className="flex items-center justify-center rounded bg-slate-elevation3 px-4 py-3 text-xs">
          Actions: {numberOfActions}
        </div>
        <div className="flex items-center justify-center rounded bg-slate-elevation3 px-4 py-3 text-xs">
          Steps: {workflowRun.total_steps ?? 0}
        </div>
      </div>
      <ScrollArea>
        <ScrollAreaViewport className="h-[37rem] max-h-[37rem]">
          <div className="space-y-4">
            {workflowRunIsNotFinalized && (
              <div
                key="stream"
                className={cn(
                  "flex cursor-pointer rounded-lg border-2 bg-slate-elevation3 p-4 hover:border-slate-50",
                  {
                    "border-slate-50": activeItem === "stream",
                  },
                )}
                onClick={onLiveStreamSelected}
              >
                <div className="flex items-center gap-2">
                  <DotFilledIcon className="h-6 w-6 text-destructive" />
                  Live
                </div>
              </div>
            )}
            {workflowRunIsFinalized && workflowRunTimeline.length === 0 && (
              <div>Workflow timeline is empty</div>
            )}
            {workflowRunTimeline?.map((timelineItem) => {
              if (isBlockItem(timelineItem)) {
                return (
                  <WorkflowRunTimelineBlockItem
                    key={timelineItem.block.workflow_run_block_id}
                    subItems={timelineItem.children}
                    activeItem={activeItem}
                    block={timelineItem.block}
                    onActionClick={onActionItemSelected}
                    onBlockItemClick={onBlockItemSelected}
                    onThoughtCardClick={onObserverThoughtCardSelected}
                  />
                );
              }
              if (isThoughtItem(timelineItem)) {
                return (
                  <ThoughtCard
                    key={timelineItem.thought.thought_id}
                    active={
                      isObserverThought(activeItem) &&
                      activeItem.thought_id === timelineItem.thought.thought_id
                    }
                    onClick={onObserverThoughtCardSelected}
                    thought={timelineItem.thought}
                  />
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
