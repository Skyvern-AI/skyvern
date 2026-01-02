import { ActionsApiResponse, Status as WorkflowRunStatus } from "@/api/types";
import { BrowserStream } from "@/components/BrowserStream";
import { AspectRatio } from "@/components/ui/aspect-ratio";
import { ActionScreenshot } from "@/routes/tasks/detail/ActionScreenshot";
import { statusIsFinalized } from "@/routes/tasks/types";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { useWorkflowRunTimelineQuery } from "../hooks/useWorkflowRunTimelineQuery";
import {
  isAction,
  isObserverThought,
  isWorkflowRunBlock,
  ObserverThought,
  WorkflowRunBlock,
} from "../types/workflowRunTypes";
import { ObserverThoughtScreenshot } from "./ObserverThoughtScreenshot";
import { WorkflowRunBlockScreenshot } from "./WorkflowRunBlockScreenshot";
import { WorkflowRunStream } from "./WorkflowRunStream";
import { useSearchParams } from "react-router-dom";
import { findActiveItem } from "./workflowTimelineUtils";
import { Skeleton } from "@/components/ui/skeleton";
import { useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

export type ActionItem = {
  block: WorkflowRunBlock;
  action: ActionsApiResponse;
};

export type WorkflowRunOverviewActiveElement =
  | ActionsApiResponse
  | ObserverThought
  | WorkflowRunBlock
  | "stream"
  | null;

function WorkflowRunOverview() {
  const [searchParams] = useSearchParams();
  const active = searchParams.get("active");
  const queryClient = useQueryClient();
  const { data: workflowRun, isLoading: workflowRunIsLoading } =
    useWorkflowRunWithWorkflowQuery();

  const { data: workflowRunTimeline, isLoading: workflowRunTimelineIsLoading } =
    useWorkflowRunTimelineQuery();

  const workflowRunId = workflowRun?.workflow_run_id;
  const workflow = workflowRun?.workflow;
  const workflowPermanentId = workflow?.workflow_permanent_id;

  const invalidateQueries = useCallback(() => {
    if (workflowRunId) {
      queryClient.invalidateQueries({
        queryKey: ["workflowRun", workflowPermanentId, workflowRunId],
      });
      queryClient.invalidateQueries({ queryKey: ["workflowRuns"] });
      queryClient.invalidateQueries({
        queryKey: ["workflowTasks", workflowRunId],
      });
      queryClient.invalidateQueries({ queryKey: ["runs"] });
    }
  }, [queryClient, workflowPermanentId, workflowRunId]);

  if (workflowRunIsLoading || workflowRunTimelineIsLoading) {
    return (
      <AspectRatio ratio={16 / 9}>
        <Skeleton className="h-full w-full" />
      </AspectRatio>
    );
  }

  if (!workflowRun) {
    return null;
  }

  if (typeof workflowRunTimeline === "undefined") {
    return null;
  }

  const workflowRunIsFinalized = statusIsFinalized(workflowRun);
  const selection = findActiveItem(
    workflowRunTimeline,
    active,
    workflowRunIsFinalized,
  );

  const browserSessionId = workflowRun.browser_session_id;

  const isPaused =
    workflowRun && workflowRun.status === WorkflowRunStatus.Paused;

  const showStreamingBrowser =
    (!workflowRunIsFinalized &&
      browserSessionId &&
      isWorkflowRunBlock(selection) &&
      selection.block_type === "human_interaction") ||
    selection === "stream";

  const shouldShowBrowserStream = !!(
    browserSessionId &&
    !workflowRunIsFinalized &&
    (selection === "stream" ||
      (isWorkflowRunBlock(selection) &&
        selection.block_type === "human_interaction"))
  );

  return (
    <AspectRatio ratio={16 / 9}>
      {shouldShowBrowserStream && (
        <BrowserStream
          key={browserSessionId}
          browserSessionId={browserSessionId}
          interactive={isPaused}
          showControlButtons={isPaused}
          workflow={undefined}
          onClose={invalidateQueries}
        />
      )}
      {!shouldShowBrowserStream && selection === "stream" && (
        <WorkflowRunStream />
      )}
      {selection !== "stream" &&
        !showStreamingBrowser &&
        isAction(selection) && (
          <ActionScreenshot
            index={selection.action_order ?? 0}
            stepId={selection.step_id ?? ""}
          />
        )}
      {isWorkflowRunBlock(selection) && !showStreamingBrowser && (
        <WorkflowRunBlockScreenshot
          workflowRunBlockId={selection.workflow_run_block_id}
        />
      )}
      {isObserverThought(selection) && (
        <ObserverThoughtScreenshot observerThoughtId={selection.thought_id} />
      )}
    </AspectRatio>
  );
}

export { WorkflowRunOverview };
