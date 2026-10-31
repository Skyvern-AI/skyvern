import { useMemo, useRef } from "react";
import { useActiveOrgId } from "@/store/ActiveOrgContext";
import { buildBlockStatusMap } from "../../studio/runProjections";

import { useWorkflowRunTimelineQuery } from "../../hooks/useWorkflowRunTimelineQuery";
import { useWorkflowRunWithWorkflowQuery } from "../../hooks/useWorkflowRunWithWorkflowQuery";
import { useStudioRunId } from "../../studio/useStudioRunId";
import {
  getRunAttempt,
  runIsLogicallyActive,
} from "../../workflowRun/runRetryState";
import { filterTimelineToAttempt } from "../../workflowRun/workflowTimelineUtils";
import type {
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../../types/workflowRunTypes";

export function collectBlocks(
  items: WorkflowRunTimelineItem[],
  into: Map<string, WorkflowRunBlock>,
  workflowRunId: string,
) {
  for (const [label, state] of Object.entries(
    buildBlockStatusMap(items, workflowRunId),
  ))
    if (!into.has(label)) into.set(label, state.block);
}

const EMPTY = new Map<string, WorkflowRunBlock>();
// Every step calls useDocRun; walk the timeline once per poll, not once per step.
const walked = new WeakMap<
  object,
  WeakMap<object, Map<string, WorkflowRunBlock>>
>();
const NO_RUN = {};
type DocRunData = ReturnType<typeof useWorkflowRunWithWorkflowQuery>["data"];

function blocksFor(
  timeline: WorkflowRunTimelineItem[],
  run: DocRunData,
  workflowRunId: string,
) {
  const runKey = run ?? NO_RUN;
  let byRun = walked.get(timeline);
  if (!byRun) walked.set(timeline, (byRun = new WeakMap()));
  let map = byRun.get(runKey);
  if (!map) {
    map = new Map();
    collectBlocks(
      filterTimelineToAttempt(
        timeline,
        run?.attempts ?? [],
        getRunAttempt(run ?? {}),
      ),
      map,
      workflowRunId,
    );
    byRun.set(runKey, map);
  }
  return map;
}

/**
 * The studio's inspected run, projected onto the Doc: the latest block row per
 * step label (a looped step keeps its last iteration), and whether the run is live.
 */
export function useDocRun() {
  const runId = useStudioRunId();
  const orgId = useActiveOrgId();
  const { data: retained, isPlaceholderData } = useWorkflowRunTimelineQuery(
    runId ? { workflowRunId: runId } : undefined,
  );
  // A status change swaps the timeline query key; keep this run's last rows meanwhile
  // instead of blanking every result, but never show another run's or organization's.
  const lastTimeline = useRef<{
    runId: string | null | undefined;
    orgId: string | undefined;
    data: typeof retained;
  } | null>(null);
  if (!isPlaceholderData && retained)
    lastTimeline.current = { runId, orgId, data: retained };
  const timeline = isPlaceholderData
    ? lastTimeline.current &&
      lastTimeline.current.runId === runId &&
      lastTimeline.current.orgId === orgId
      ? lastTimeline.current.data
      : undefined
    : retained;
  const { data: run } = useWorkflowRunWithWorkflowQuery({
    workflowRunId: runId ?? undefined,
  });
  const blocksByLabel = useMemo(
    () => (timeline && runId ? blocksFor(timeline, run, runId) : EMPTY),
    [timeline, run, runId],
  );
  return {
    runId,
    run,
    blocksByLabel,
    live: Boolean(run && runIsLogicallyActive(run)),
  };
}
