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

type Props = {
  activeItem: WorkflowRunOverviewActiveElement;
  onObserverThoughtCardSelected: (item: ObserverThought) => void;
  onActionItemSelected: (item: ActionItem) => void;
  onBlockItemSelected: (item: WorkflowRunBlock) => void;
};

function WorkflowDebuggerRunTimeline({
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
    return (
      <div className="flex h-full w-full flex-col items-center justify-center rounded-xl bg-[#020817] p-12">
        <div className="flex h-full w-full flex-col items-center justify-center gap-4">
          <div>
            Hi! 👋 We're experimenting with a new feature called debugger.
          </div>
          <div>
            This debugger allows you to see the state of your workflow in a live
            browser.
          </div>
          <div>
            You can run individual blocks, instead of the whole workflow.
          </div>
          <div>
            To get started, press the play button on a block in your workflow.
          </div>
        </div>
      </div>
    );
  }

  const workflowRunIsFinalized = statusIsFinalized(workflowRun);

  const numberOfActions = workflowRunTimeline.reduce((total, current) => {
    if (isTaskVariantBlockItem(current)) {
      return total + current.block!.actions!.length;
    }
    return total + 0;
  }, 0);

  return (
    <div className="w-full min-w-0 space-y-4 rounded bg-slate-elevation1 p-4">
      <div className="grid w-full grid-cols-2 gap-2">
        <div className="flex items-center justify-center rounded bg-slate-elevation3 px-4 py-3 text-xs">
          Actions: {numberOfActions}
        </div>
        <div className="flex items-center justify-center rounded bg-slate-elevation3 px-4 py-3 text-xs">
          Steps: {workflowRun.total_steps ?? 0}
        </div>
      </div>
      {!workflowRunIsFinalized && workflowRunTimeline.length === 0 && (
        <Skeleton className="h-full w-full" />
      )}
      <ScrollArea>
        <ScrollAreaViewport className="h-full w-full">
          <div className="w-full space-y-4">
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

export { WorkflowDebuggerRunTimeline };
