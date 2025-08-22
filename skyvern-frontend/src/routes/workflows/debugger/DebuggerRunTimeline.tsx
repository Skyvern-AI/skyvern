import { useParams } from "react-router-dom";
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
import { useWorkflowQuery } from "../hooks/useWorkflowQuery";

type Props = {
  activeItem: WorkflowRunOverviewActiveElement;
  onObserverThoughtCardSelected: (item: ObserverThought) => void;
  onActionItemSelected: (item: ActionItem) => void;
  onBlockItemSelected: (item: WorkflowRunBlock) => void;
};

function Step({ n, children }: { n: number; children: React.ReactNode }) {
  return (
    <div className="relative flex items-center justify-center gap-2 rounded-lg border border-slate-600 p-4">
      <div className="absolute right-[-1.22rem] top-[-1.22rem] flex h-[3rem] w-[3rem] items-center justify-center rounded-full border border-slate-600 bg-slate-elevation3 px-4 py-3 text-xl font-bold">
        {n}
      </div>
      <div className="absolute right-[-1.25rem] top-[-1.25rem] flex h-[3rem] w-[3rem] items-center justify-center rounded-full bg-slate-elevation3 px-4 py-3 text-xl font-bold text-slate-100">
        {n}
      </div>
      <div className="flex-1">{children}</div>
    </div>
  );
}

function DebuggerRunTimeline({
  activeItem,
  onObserverThoughtCardSelected,
  onActionItemSelected,
  onBlockItemSelected,
}: Props) {
  const { workflowPermanentId } = useParams();
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId }!);
  const { data: workflowRun, isLoading: workflowRunIsLoading } =
    useWorkflowRunQuery();

  const { data: workflowRunTimeline, isLoading: workflowRunTimelineIsLoading } =
    useWorkflowRunTimelineQuery();

  if (workflowRunIsLoading || workflowRunTimelineIsLoading) {
    return <Skeleton className="h-full w-full" />;
  }

  const blocks = workflow?.workflow_definition.blocks ?? [];

  const getStarted =
    blocks.length === 0 ? (
      <div>
        Hi! 👋 To get started, add a block to your workflow. You can do that by
        clicking the round plus button beneath the Start block, on the left
      </div>
    ) : null;

  const runABlock = (
    <div>
      To run a single block, click the play button on that block. Skyvern will
      run the block in the browser, live!
    </div>
  );

  const adjustBrowser = (
    <div>
      Need to adjust the browser to test your block again? You can click around
      in the browser to bring Skyvern to any page (manually!)
    </div>
  );

  const parameters = (
    <div>
      Want Skyvern to do different things based on your inputs? Use Parameters
      to specify them and reference them using <code>{"{{ }}"}</code> syntax!
    </div>
  );

  const addBlocks = (
    <div>
      Not finished? Add a block to your workflow by clicking the round plus
      button before or after any other block.
    </div>
  );

  const steps = [
    getStarted,
    runABlock,
    adjustBrowser,
    getStarted === null ? parameters : null,
    getStarted === null ? addBlocks : null,
  ].filter((step) => step);

  if (!workflowRun || !workflowRunTimeline) {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center overflow-y-auto rounded-xl bg-[#020817] p-8 text-slate-300">
        <div className="flex h-full w-full flex-col items-center justify-around gap-4">
          <div className="text-center text-xl">
            Build & Debug Complex Browser Automations
          </div>
          {steps.map((step, index) => (
            <Step key={index} n={index + 1}>
              {step}
            </Step>
          ))}
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

export { DebuggerRunTimeline };
