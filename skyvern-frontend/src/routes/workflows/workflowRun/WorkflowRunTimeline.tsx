import { Skeleton } from "@/components/ui/skeleton";
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
import {
  ActionItem,
  WorkflowRunOverviewActiveElement,
} from "./WorkflowRunOverview";
import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import { statusIsNotFinalized } from "@/routes/tasks/types";
import { cn } from "@/util/utils";
import { ThoughtCard } from "./ThoughtCard";
import { WorkflowRunTimelineBlockItem } from "./WorkflowRunTimelineBlockItem";
import { DotFilledIcon } from "@radix-ui/react-icons";

const formatter = Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
});

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
    useWorkflowRunQuery();

  const { data: workflowRunTimeline, isLoading: workflowRunTimelineIsLoading } =
    useWorkflowRunTimelineQuery();

  if (workflowRunIsLoading || workflowRunTimelineIsLoading) {
    return <Skeleton className="h-full w-full" />;
  }

  if (!workflowRun || !workflowRunTimeline) {
    return null;
  }

  const workflowRunIsNotFinalized = statusIsNotFinalized(workflowRun);

  const numberOfActions = workflowRunTimeline.reduce((total, current) => {
    if (isTaskVariantBlockItem(current)) {
      return total + current.block!.actions!.length;
    }
    return total + 0;
  }, 0);

  return (
    <div className="min-w-0 space-y-4 rounded bg-slate-elevation1 p-4">
      <div className="grid grid-cols-3 gap-2">
        <div className="flex items-center justify-center rounded bg-slate-elevation3 px-4 py-3 text-xs">
          Actions: {numberOfActions}
        </div>
        <div className="flex items-center justify-center rounded bg-slate-elevation3 px-4 py-3 text-xs">
          Steps: {workflowRun.total_steps ?? 0}
        </div>
        <div className="flex items-center justify-center rounded bg-slate-elevation3 px-4 py-3 text-xs">
          Cost: {formatter.format(workflowRun.total_cost ?? 0)}
        </div>
      </div>
      <ScrollArea>
        <ScrollAreaViewport className="max-h-[37rem]">
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
            {workflowRunTimeline.length === 0 && (
              <div>Workflow timeline is empty</div>
            )}
            {workflowRunTimeline?.map((timelineItem) => {
              if (isBlockItem(timelineItem)) {
                return (
                  <WorkflowRunTimelineBlockItem
                    key={timelineItem.block.workflow_run_block_id}
                    subBlocks={timelineItem.children
                      .filter((item) => item.type === "block")
                      .map((item) => item.block)}
                    activeItem={activeItem}
                    block={timelineItem.block}
                    onActionClick={onActionItemSelected}
                    onBlockItemClick={onBlockItemSelected}
                  />
                );
              }
              if (isThoughtItem(timelineItem)) {
                return (
                  <ThoughtCard
                    key={timelineItem.thought.observer_thought_id}
                    active={
                      isObserverThought(activeItem) &&
                      activeItem.observer_thought_id ===
                        timelineItem.thought.observer_thought_id
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
