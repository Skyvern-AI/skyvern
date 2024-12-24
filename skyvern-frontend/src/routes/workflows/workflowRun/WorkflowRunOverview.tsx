import { AspectRatio } from "@/components/ui/aspect-ratio";
import { useWorkflowRunQuery } from "../hooks/useWorkflowRunQuery";
import { WorkflowRunOverviewSkeleton } from "./WorkflowRunOverviewSkeleton";
import { useState } from "react";
import { statusIsNotFinalized } from "@/routes/tasks/types";
import { useWorkflowRunTimelineQuery } from "../hooks/useWorkflowRunTimelineQuery";
import { WorkflowRunStream } from "./WorkflowRunStream";
import { ActionScreenshot } from "@/routes/tasks/detail/ActionScreenshot";
import { WorkflowRunTimelineBlockItem } from "./WorkflowRunTimelineBlockItem";
import { ThoughtCard } from "./ThoughtCard";
import {
  isAction,
  isBlockItem,
  isObserverThought,
  isThoughtItem,
  isWorkflowRunBlock,
  ObserverThought,
  WorkflowRunBlock,
} from "../types/workflowRunTypes";
import { ActionsApiResponse } from "@/api/types";
import { cn } from "@/util/utils";
import { DotFilledIcon } from "@radix-ui/react-icons";
import { WorkflowRunTimelineItemInfoSection } from "./WorkflowRunTimelineItemInfoSection";
import { ObserverThoughtScreenshot } from "./ObserverThoughtScreenshot";
import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";

export type WorkflowRunOverviewActiveElement =
  | ActionsApiResponse
  | ObserverThought
  | WorkflowRunBlock
  | "stream"
  | null;

function WorkflowRunOverview() {
  const [active, setActive] = useState<WorkflowRunOverviewActiveElement>(null);
  const { data: workflowRun, isLoading: workflowRunIsLoading } =
    useWorkflowRunQuery();

  const { data: workflowRunTimeline, isLoading: workflowRunTimelineIsLoading } =
    useWorkflowRunTimelineQuery();

  if (workflowRunIsLoading || workflowRunTimelineIsLoading) {
    return <WorkflowRunOverviewSkeleton />;
  }

  if (!workflowRun) {
    return null;
  }

  if (typeof workflowRunTimeline === "undefined") {
    return null;
  }

  const workflowRunIsNotFinalized = statusIsNotFinalized(workflowRun);

  const timeline = workflowRunTimeline.slice().reverse();

  function getActiveSelection(): WorkflowRunOverviewActiveElement {
    if (active === null) {
      if (workflowRunIsNotFinalized) {
        return "stream";
      }
      if (timeline!.length > 0) {
        const timelineItem = timeline![0];
        if (isBlockItem(timelineItem)) {
          if (
            timelineItem.block.actions &&
            timelineItem.block.actions.length > 0
          ) {
            return timelineItem.block
              .actions[0] as WorkflowRunOverviewActiveElement;
          }
          return timelineItem.block;
        }
        if (isThoughtItem(timelineItem)) {
          return timelineItem.thought;
        }
      }
    }
    return active;
  }

  const selection = getActiveSelection();

  return (
    <div className="flex h-[42rem] gap-6">
      <div className="w-2/3 space-y-4">
        <AspectRatio ratio={16 / 9} className="overflow-y-hidden">
          {selection === "stream" && <WorkflowRunStream />}
          {selection !== "stream" && isAction(selection) && (
            <ActionScreenshot
              index={selection.action_order ?? 0}
              stepId={selection.step_id ?? ""}
            />
          )}
          {isWorkflowRunBlock(selection) && (
            <div className="flex h-full w-full items-center justify-center bg-slate-elevation1">
              No screenshot found for this block
            </div>
          )}
          {isObserverThought(selection) && (
            <ObserverThoughtScreenshot
              observerThoughtId={selection.observer_thought_id}
            />
          )}
        </AspectRatio>

        <WorkflowRunTimelineItemInfoSection item={selection} />
      </div>
      <div className="w-1/3 min-w-0 rounded bg-slate-elevation1 p-4">
        <ScrollArea>
          <ScrollAreaViewport className="max-h-[42rem]">
            <div className="space-y-4">
              <div className="gap-2"></div>
              {workflowRunIsNotFinalized && (
                <div
                  key="stream"
                  className={cn(
                    "flex cursor-pointer rounded-lg border-2 bg-slate-elevation3 p-4 hover:border-slate-50",
                    {
                      "border-slate-50": selection === "stream",
                    },
                  )}
                  onClick={() => setActive("stream")}
                >
                  <div className="flex items-center gap-2">
                    <DotFilledIcon className="h-6 w-6 text-destructive" />
                    Live
                  </div>
                </div>
              )}
              {timeline.length === 0 && <div>Workflow timeline is empty</div>}
              {timeline?.map((timelineItem) => {
                if (isBlockItem(timelineItem)) {
                  return (
                    <WorkflowRunTimelineBlockItem
                      key={timelineItem.block.workflow_run_block_id}
                      subBlocks={timelineItem.children
                        .filter((item) => item.type === "block")
                        .map((item) => item.block)}
                      activeItem={selection}
                      block={timelineItem.block}
                      onActionClick={setActive}
                      onBlockItemClick={setActive}
                    />
                  );
                }
                if (isThoughtItem(timelineItem)) {
                  return (
                    <ThoughtCard
                      key={timelineItem.thought.observer_thought_id}
                      active={
                        isObserverThought(selection) &&
                        selection.observer_thought_id ===
                          timelineItem.thought.observer_thought_id
                      }
                      onClick={setActive}
                      thought={timelineItem.thought}
                    />
                  );
                }
              })}
            </div>
          </ScrollAreaViewport>
        </ScrollArea>
      </div>
    </div>
  );
}

export { WorkflowRunOverview };
