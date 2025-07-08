import { ActionsApiResponse } from "@/api/types";
import { BrowserStream } from "@/components/BrowserStream";
import { AspectRatio } from "@/components/ui/aspect-ratio";
import { ActionScreenshot } from "@/routes/tasks/detail/ActionScreenshot";
import { statusIsFinalized } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "../hooks/useWorkflowRunQuery";
import { useParams } from "react-router-dom";
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
  const { workflowPermanentId } = useParams<{
    workflowPermanentId: string;
  }>();
  const queryClient = useQueryClient();
  const { data: workflowRun, isLoading: workflowRunIsLoading } =
    useWorkflowRunQuery();

  const { data: workflowRunTimeline, isLoading: workflowRunTimelineIsLoading } =
    useWorkflowRunTimelineQuery();

  const invalidateQueries = useCallback(() => {
    if (workflowRun) {
      queryClient.invalidateQueries({
        queryKey: [
          "workflowRun",
          workflowPermanentId,
          workflowRun.workflow_run_id,
        ],
      });
      queryClient.invalidateQueries({ queryKey: ["workflowRuns"] });
      queryClient.invalidateQueries({
        queryKey: ["workflowTasks", workflowRun.workflow_run_id],
      });
      queryClient.invalidateQueries({ queryKey: ["runs"] });
    }
  }, [queryClient, workflowPermanentId, workflowRun]);

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

  const streamingComponent = workflowRun.browser_session_id ? (
    <BrowserStream
      workflow={{ run: workflowRun }}
      onClose={() => invalidateQueries()}
    />
  ) : (
    <WorkflowRunStream />
  );

  return (
    <AspectRatio ratio={16 / 9}>
      {selection === "stream" && streamingComponent}
      {selection !== "stream" && isAction(selection) && (
        <ActionScreenshot
          index={selection.action_order ?? 0}
          stepId={selection.step_id ?? ""}
        />
      )}
      {isWorkflowRunBlock(selection) && (
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
