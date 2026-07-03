import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";

import { Status, WorkflowRunStatusApiResponseWithWorkflow } from "@/api/types";
import { statusIsFinalized } from "@/routes/tasks/types";
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
  findActiveItem,
  findTimelineBlock,
  resolveScreenshotBlockId,
} from "../workflowRun/workflowTimelineUtils";
import { type HeroSelection } from "./runview/HeroScreenshot";
import {
  actionLabel,
  buildFilmstrip,
  runOutcomeFromStatus,
} from "./runProjections";

export type RunVisuals = {
  workflowRun: WorkflowRunStatusApiResponseWithWorkflow | undefined;
  timeline: WorkflowRunTimelineItem[] | undefined;
  running: boolean;
  failed: boolean;
  finalized: boolean;
  provisioning: boolean;
  isPaused: boolean;
  recordingUrls: string[];
  recordingArchived: boolean;
  hasScreenshots: boolean;
  // ?active= pins a specific step (anything but the live-edge "stream" pin).
  scrubbing: boolean;
  heroSelection: HeroSelection | null;
  heroLabel: string;
};

function hasScreenshotCandidate(selection: HeroSelection | null): boolean {
  if (!selection) {
    return false;
  }
  if (selection.kind === "action") {
    return Boolean(
      selection.artifactId ||
      (selection.stepId && selection.actionOrder != null),
    );
  }
  return true;
}

/**
 * The inspected run's visual state for the Browser pane, derived the same way
 * RunView drives its hero: ?active= (or the live edge) picks the timeline item,
 * and screenshots resolve per element kind (action → own artifact, container
 * block → leaf block, thought → LLM screenshot).
 */
export function useRunVisuals(workflowRunId: string | undefined): RunVisuals {
  const queryOptions = workflowRunId ? { workflowRunId } : undefined;
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery(queryOptions);
  const { data: timeline } = useWorkflowRunTimelineQuery(queryOptions);
  const [searchParams] = useSearchParams();
  const activeParam = searchParams.get("active");
  // The Timeline pane's loop-iteration selection isn't in the URL; read it from the
  // shared store so a selected iteration's screenshot resolves here too.
  const activeIteration = useRunViewStore((s) => s.activeIteration);

  const outcome = runOutcomeFromStatus(workflowRun?.status);
  const running = outcome === "running";
  // A user-canceled run isn't a failure — its replay defaults like a success.
  const canceled = workflowRun?.status === Status.Canceled;
  const failed = outcome === "failed" && !canceled;
  const finalized = workflowRun ? statusIsFinalized(workflowRun) : false;
  const provisioning =
    workflowRun?.status === Status.Created ||
    workflowRun?.status === Status.Queued;
  const isPaused = workflowRun?.status === Status.Paused;

  const recordingUrls = useMemo(
    () => getRecordingUrls(workflowRun),
    [workflowRun],
  );

  const frames = useMemo(() => buildFilmstrip(timeline), [timeline]);
  const lastFrame = frames.length > 0 ? frames[frames.length - 1] : null;
  const scrubbing = activeParam != null && activeParam !== "stream";
  const selectedFrameId = scrubbing ? activeParam : (lastFrame?.id ?? null);
  const finallyBlockLabel =
    workflowRun?.workflow?.workflow_definition?.finally_block_label ?? null;
  const activeItem = useMemo(
    () =>
      findActiveItem(
        timeline ?? [],
        selectedFrameId,
        finalized,
        finallyBlockLabel,
      ),
    [timeline, selectedFrameId, finalized, finallyBlockLabel],
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
  const hasScreenshots =
    hasScreenshotFrame || hasScreenshotCandidate(heroSelection);

  const heroLabel = isAction(activeItem)
    ? actionLabel(activeItem)
    : isWorkflowRunBlock(activeItem)
      ? (activeItem.label ?? "Screenshot")
      : isObserverThought(activeItem)
        ? (activeItem.thought ?? "Thought")
        : "Screenshot";

  return {
    workflowRun,
    timeline,
    running,
    failed,
    finalized,
    provisioning,
    isPaused,
    recordingUrls,
    recordingArchived: workflowRun?.recording_archived ?? false,
    hasScreenshots,
    scrubbing,
    heroSelection,
    heroLabel,
  };
}
