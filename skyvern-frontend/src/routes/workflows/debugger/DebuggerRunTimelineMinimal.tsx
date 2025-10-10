import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { statusIsFinalized } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "../hooks/useWorkflowRunQuery";
import { useWorkflowRunTimelineQuery } from "../hooks/useWorkflowRunTimelineQuery";
import { isBlockItem, isThoughtItem } from "../types/workflowRunTypes";
import { ThoughtCardMinimal } from "@/routes/workflows/workflowRun/ThoughtCardMinimal";
import { WorkflowRunTimelineBlockItemMinimal } from "@/routes/workflows/workflowRun/WorkflowRunTimelineBlockItemMinimal";
import { cn } from "@/util/utils";

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
        <Skeleton className="vertical-line-gradient-soft flex h-full min-h-[30rem] w-full items-center justify-center overflow-visible">
          {/* rotate this by 90 degrees */}
          <div
            className="flex h-full w-full items-center justify-center overflow-visible opacity-50"
            style={{ writingMode: "vertical-rl" }}
          >
            formulating actions...
          </div>
        </Skeleton>
      )}
      <ScrollArea className="h-full w-full">
        <ScrollAreaViewport className="h-full w-full">
          <div className="flex w-full flex-col items-center justify-center gap-4 pt-2">
            {workflowRunIsFinalized && workflowRunTimeline.length === 0 && (
              <div>-</div>
            )}
            {workflowRunTimeline?.map((timelineItem, i) => {
              if (isBlockItem(timelineItem)) {
                return (
                  <div
                    key={timelineItem.block.workflow_run_block_id}
                    className={cn({
                      "animate-pulse": !workflowRunIsFinalized && i === 0,
                    })}
                  >
                    <WorkflowRunTimelineBlockItemMinimal
                      subItems={timelineItem.children}
                      block={timelineItem.block}
                    />
                  </div>
                );
              }
              if (isThoughtItem(timelineItem)) {
                return (
                  <div
                    key={timelineItem.thought.thought_id}
                    className={cn({
                      "animate-pulse": !workflowRunIsFinalized && i === 0,
                    })}
                  >
                    <ThoughtCardMinimal
                      key={timelineItem.thought.thought_id}
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

export { DebuggerRunTimelineMinimal };
