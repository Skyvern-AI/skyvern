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
  isActionItem,
  isBlockItem,
  isObserverThought,
  isTaskVariantBlockItem,
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
import { WorkflowRunBlockScreenshot } from "./WorkflowRunBlockScreenshot";

const formatter = Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
});

export type ActionItem = {
  block: WorkflowRunBlock;
  action: ActionsApiResponse;
};

export type WorkflowRunOverviewActiveElement =
  | ActionItem
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
            const last = timelineItem.block.actions.length - 1;
            const actionItem: ActionItem = {
              block: timelineItem.block,
              action: timelineItem.block.actions[last]!,
            };
            return actionItem;
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

  const numberOfActions = workflowRunTimeline.reduce((total, current) => {
    if (isTaskVariantBlockItem(current)) {
      return total + current.block!.actions!.length;
    }
    return total + 0;
  }, 0);

  return (
    <div className="flex h-[42rem] gap-6">
      <div className="w-2/3 space-y-4">
        <AspectRatio ratio={16 / 9} className="overflow-y-hidden">
          {selection === "stream" && <WorkflowRunStream />}
          {selection !== "stream" && isActionItem(selection) && (
            <ActionScreenshot
              index={selection.action.action_order ?? 0}
              stepId={selection.action.step_id ?? ""}
            />
          )}
          {isWorkflowRunBlock(selection) && (
            <WorkflowRunBlockScreenshot
              workflowRunBlockId={selection.workflow_run_block_id}
            />
          )}
          {isObserverThought(selection) && (
            <ObserverThoughtScreenshot
              observerThoughtId={selection.observer_thought_id}
            />
          )}
        </AspectRatio>

        <WorkflowRunTimelineItemInfoSection activeItem={selection} />
      </div>
      <div className="w-1/3 min-w-0 space-y-4 rounded bg-slate-elevation1 p-4">
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
