import {
  getRunAttempt,
  runIsLogicallyActive,
  runIsLogicallyFinal,
  runIsExecuting,
} from "@/routes/workflows/workflowRun/runRetryState";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { Status, WorkflowRunStatusApiResponseWithWorkflow } from "@/api/types";

import {
  isAction,
  isObserverThought,
  isWorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "@/routes/workflows/types/workflowRunTypes";
import { useRunViewStore } from "@/store/RunViewStore";

import { useWorkflowRunTimelineQuery } from "../hooks/useWorkflowRunTimelineQuery";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { getRecordingUrls } from "../workflowRun/recordingUrls";
import {
  filterTimelineToAttempt,
  findActiveItem,
  findTimelineBlock,
  resolveScreenshotBlockId,
} from "../workflowRun/workflowTimelineUtils";
import { type HeroSelection } from "./runview/HeroScreenshot";
import { useHeroScreenshot } from "./runview/useHeroScreenshot";
import {
  buildFilmstrip,
  resolveLandingSelectionId,
  runOutcomeFromStatus,
} from "./runProjections";

export type RunVisuals = {
  workflowRun: WorkflowRunStatusApiResponseWithWorkflow | undefined;
  timeline: WorkflowRunTimelineItem[] | undefined;
  running: boolean;
  executing: boolean;
  failed: boolean;
  finalized: boolean;
  provisioning: boolean;
  isPaused: boolean;
  recordingUrls: string[];
  recordingArchived: boolean;
  hasScreenshots: boolean;
  // Not yet known: the timeline or the selected block/thought's artifacts are loading.
  screenshotsPending: boolean;
  // ?active= pins a specific step (anything but the live-edge "stream" pin).
  scrubbing: boolean;
  heroSelection: HeroSelection | null;
};

function hasActionScreenshot(selection: HeroSelection | null): boolean {
  return (
    selection?.kind === "action" &&
    Boolean(
      selection.artifactId ||
      (selection.stepId && selection.actionOrder != null),
    )
  );
}

/**
 * The inspected run's visual state for the Browser pane, derived the same way
 * RunView drives its hero: ?active= (or the live edge) picks the timeline item,
 * and screenshots resolve per element kind (action → own artifact, container
 * block → leaf block, thought → LLM screenshot).
 */
export function useRunVisuals(workflowRunId: string | undefined): RunVisuals {
  const queryOptions = { workflowRunId };
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery(queryOptions);
  const {
    data: retainedTimeline,
    isPlaceholderData: timelineIsPlaceholder,
    isFetching: timelineFetching,
  } = useWorkflowRunTimelineQuery(queryOptions);
  // The timeline payload carries no run id of its own, so keepPreviousData serves
  // the previous run's timeline on both a switch and a clear. It also bridges this
  // run's own refetch on a status change (status is in the query key); those rows
  // stay, or replay pills blink off mid-run.
  const [loadedTimelineRunId, setLoadedTimelineRunId] = useState<string>();
  if (
    workflowRunId &&
    !timelineIsPlaceholder &&
    retainedTimeline !== undefined &&
    loadedTimelineRunId !== workflowRunId
  ) {
    setLoadedTimelineRunId(workflowRunId);
  }
  const timeline =
    !workflowRunId ||
    (timelineIsPlaceholder && loadedTimelineRunId !== workflowRunId)
      ? undefined
      : retainedTimeline;
  const currentTimeline = useMemo(
    () =>
      timeline
        ? filterTimelineToAttempt(
            timeline,
            workflowRun?.attempts ?? [],
            getRunAttempt(workflowRun ?? {}),
          )
        : undefined,
    [timeline, workflowRun],
  );
  const [searchParams] = useSearchParams();
  const activeParam = searchParams.get("active");
  // The Overview pane's loop-iteration selection isn't in the URL; read it from the
  // shared store so a selected iteration's screenshot resolves here too.
  const activeIteration = useRunViewStore((s) => s.activeIteration);

  const outcome = runOutcomeFromStatus(workflowRun);
  const running = Boolean(workflowRun && runIsLogicallyActive(workflowRun));
  const executing = Boolean(workflowRun && runIsExecuting(workflowRun));
  // A user-canceled run isn't a failure — its replay defaults like a success.
  const canceled = workflowRun?.status === Status.Canceled;
  const failed = outcome === "failed" && !canceled;
  const finalized = workflowRun ? runIsLogicallyFinal(workflowRun) : false;
  const provisioning =
    workflowRun?.status === Status.Created ||
    workflowRun?.status === Status.Queued;
  const isPaused = workflowRun?.status === Status.Paused;

  const recordingUrls = useMemo(
    () => getRecordingUrls(workflowRun),
    [workflowRun],
  );

  const frames = useMemo(
    () => buildFilmstrip(currentTimeline),
    [currentTimeline],
  );
  const scrubbing = activeParam != null && activeParam !== "stream";
  const landingSelectionId = useMemo(
    () => resolveLandingSelectionId(frames, currentTimeline, finalized),
    [frames, currentTimeline, finalized],
  );
  const selectedFrameId = scrubbing ? activeParam : landingSelectionId;
  const finallyBlockLabel =
    workflowRun?.workflow?.workflow_definition?.finally_block_label ?? null;
  const activeItem = useMemo(
    () =>
      findActiveItem(
        (selectedFrameId ? timeline : currentTimeline) ?? [],
        selectedFrameId,
        finalized,
        finallyBlockLabel,
      ),
    [timeline, currentTimeline, selectedFrameId, finalized, finallyBlockLabel],
  );

  const heroSelection = useMemo<HeroSelection | null>(() => {
    if (isAction(activeItem)) {
      return {
        kind: "action",
        artifactId: activeItem.screenshot_artifact_id ?? null,
        stepId: activeItem.step_id ?? null,
        actionOrder: activeItem.action_order ?? null,
      };
    }
    if (isWorkflowRunBlock(activeItem)) {
      const screenshotBlockId = resolveScreenshotBlockId(
        timeline ?? [],
        activeItem,
        activeIteration,
      );
      const blockType =
        findTimelineBlock(timeline ?? [], screenshotBlockId)?.block_type ??
        activeItem.block_type ??
        null;
      return {
        kind: "block",
        workflowRunBlockId: screenshotBlockId,
        blockType,
      };
    }
    if (isObserverThought(activeItem)) {
      return { kind: "thought", thoughtId: activeItem.thought_id };
    }
    return null;
  }, [activeItem, timeline, activeIteration]);

  const hasScreenshotFrame = useMemo(
    () =>
      frames.some(
        (frame) =>
          frame.screenshotArtifactId != null ||
          (frame.stepId != null && frame.actionOrder != null),
      ),
    [frames],
  );
  // A selected block or thought offers Screenshots only once its artifacts resolve
  // to one (the same lookup the hero renders); otherwise the pill opens nothing.
  const { screenshot: selectionScreenshot, isLoading: selectionLoading } =
    useHeroScreenshot(
      heroSelection?.kind === "action" ? null : heroSelection,
      executing,
    );
  const hasScreenshots =
    heroSelection?.kind === "block" || heroSelection?.kind === "thought"
      ? Boolean(selectionScreenshot && !selectionScreenshot.archived)
      : hasScreenshotFrame || hasActionScreenshot(heroSelection);
  const screenshotsPending =
    !hasScreenshots &&
    (selectionLoading || (timelineFetching && timeline === undefined));

  return {
    workflowRun,
    timeline,
    running,
    executing,
    failed,
    finalized,
    provisioning,
    isPaused,
    recordingUrls,
    recordingArchived: workflowRun?.recording_archived ?? false,
    hasScreenshots,
    screenshotsPending,
    scrubbing,
    heroSelection,
  };
}
