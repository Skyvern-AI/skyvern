import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { statusIsFinalized } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "../hooks/useWorkflowRunQuery";
import { useWorkflowRunTimelineQuery } from "../hooks/useWorkflowRunTimelineQuery";
import { isBlockItem, isThoughtItem } from "../types/workflowRunTypes";
import { ThoughtCardMinimal } from "@/routes/workflows/workflowRun/ThoughtCardMinimal";
import { WorkflowRunTimelineBlockItemMinimal } from "@/routes/workflows/workflowRun/WorkflowRunTimelineBlockItemMinimal";

function DebuggerRunTimelineMinimal() {
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

  return (
    <div className="h-full w-full">
      {!workflowRunIsFinalized && workflowRunTimeline.length === 0 && (
        <Skeleton className="h-full w-full" />
      )}
      <ScrollArea className="h-full w-full">
        <ScrollAreaViewport className="h-full w-full">
          <div className="flex w-full flex-col items-center justify-center gap-4 pt-2">
            {workflowRunIsFinalized && workflowRunTimeline.length === 0 && (
              <div>-</div>
            )}
            {workflowRunTimeline?.map((timelineItem) => {
              if (isBlockItem(timelineItem)) {
                return (
                  <WorkflowRunTimelineBlockItemMinimal
                    key={timelineItem.block.workflow_run_block_id}
                    subItems={timelineItem.children}
                    block={timelineItem.block}
                  />
                );
              }
              if (isThoughtItem(timelineItem)) {
                return (
                  <ThoughtCardMinimal
                    key={timelineItem.thought.thought_id}
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

export { DebuggerRunTimelineMinimal };
