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
import {
  findActiveItem,
  findTimelineBlock,
  parseActiveIterationParam,
  resolveScreenshotBlockId,
} from "./workflowTimelineUtils";
import { Skeleton } from "@/components/ui/skeleton";
import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { useBrowserStreamingMode } from "@/hooks/useRuntimeConfig";
import { RunHealChip } from "./RunHealChip";
import { RunReliabilityUplink } from "./RunReliabilityUplink";

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
  const iterationParam = searchParams.get("iteration");
  const activeIteration = parseActiveIterationParam(iterationParam);
  const queryClient = useQueryClient();
  const [vncFailed, setVncFailed] = useState(false);
  const { browserStreamingMode } = useBrowserStreamingMode();
  const { data: workflowRun, isLoading: workflowRunIsLoading } =
    useWorkflowRunWithWorkflowQuery();

  const { data: workflowRunTimeline, isLoading: workflowRunTimelineIsLoading } =
    useWorkflowRunTimelineQuery();

  const workflowRunId = workflowRun?.workflow_run_id;
  const workflow = workflowRun?.workflow;
  const workflowPermanentId = workflow?.workflow_permanent_id;

  const browserSessionId = workflowRun?.browser_session_id;

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

  const handleVncClose = useCallback(() => {
    setVncFailed(true);
    invalidateQueries();
  }, [invalidateQueries]);

  useEffect(() => {
    setVncFailed(false);
  }, [browserSessionId]);

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
  const finallyBlockLabel =
    workflowRun.workflow?.workflow_definition?.finally_block_label ?? null;
  const selection = findActiveItem(
    workflowRunTimeline,
    active,
    workflowRunIsFinalized,
    finallyBlockLabel,
  );

  const isPaused =
    workflowRun && workflowRun.status === WorkflowRunStatus.Paused;

  const wantsVncStream = !!(
    browserSessionId &&
    !workflowRunIsFinalized &&
    (selection === "stream" ||
      (isWorkflowRunBlock(selection) &&
        selection.block_type === "human_interaction"))
  );

  const shouldUseCdpStream = browserStreamingMode === "cdp";
  const shouldShowBrowserStream =
    wantsVncStream && !shouldUseCdpStream && !vncFailed;
  const shouldShowScreencastFallback =
    wantsVncStream && (shouldUseCdpStream || vncFailed);

  const isStreamActive =
    shouldShowBrowserStream ||
    shouldShowScreencastFallback ||
    selection === "stream";

  return (
    <AspectRatio ratio={16 / 9}>
      <div className="relative h-full w-full">
        <div className="absolute left-2 top-2 z-20 flex flex-col gap-1">
          <RunHealChip workflowRunId={workflowRunId} />
          <RunReliabilityUplink workflowRunId={workflowRunId} />
        </div>
        {shouldShowBrowserStream && (
          <BrowserStream
            key={browserSessionId}
            browserSessionId={browserSessionId!}
            interactive={isPaused}
            showControlButtons={isPaused}
            workflow={undefined}
            onClose={handleVncClose}
          />
        )}
        {!shouldShowBrowserStream &&
          (shouldShowScreencastFallback || selection === "stream") && (
            <WorkflowRunStream
              alwaysShowStream={shouldShowScreencastFallback}
              interactive={isPaused}
              showControlButtons={isPaused}
            />
          )}
        {!isStreamActive && isAction(selection) && (
          <ActionScreenshot
            artifactId={selection.screenshot_artifact_id ?? undefined}
            index={selection.action_order ?? 0}
            stepId={selection.step_id ?? ""}
          />
        )}
        {isWorkflowRunBlock(selection) &&
          !isStreamActive &&
          (() => {
            // A container selection (loop/conditional) resolves to a descendant leaf, so read the
            // leaf's type rather than the container's so a nested code block is treated as one.
            const screenshotBlockId = resolveScreenshotBlockId(
              workflowRunTimeline,
              selection,
              activeIteration,
            );
            const screenshotBlockType =
              findTimelineBlock(workflowRunTimeline, screenshotBlockId)
                ?.block_type ?? selection.block_type;
            return (
              <WorkflowRunBlockScreenshot
                workflowRunBlockId={screenshotBlockId}
                blockType={screenshotBlockType}
                runStatus={workflowRun?.status}
              />
            );
          })()}
        {isObserverThought(selection) && (
          <ObserverThoughtScreenshot observerThoughtId={selection.thought_id} />
        )}
      </div>
    </AspectRatio>
  );
}

export { WorkflowRunOverview };
