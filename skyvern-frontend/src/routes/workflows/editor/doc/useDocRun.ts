import { useEffect, useMemo, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { AxiosError } from "axios";
import { getClient } from "@/api/AxiosClient";
import type { WorkflowRunApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";
import { useWorkflowPermanentId } from "../../WorkflowPermanentIdContext";
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
import {
  collectLoopProgress,
  sameSteps,
  stepLabelsOf,
  stepsUnknownToRun,
  type DocLoopProgress,
} from "./docRunState";
import { useWorkflowQuery } from "../../hooks/useWorkflowQuery";
import {
  useDismissedDocRunId,
  isStepRun,
  markStepRun,
  useDocRunStore,
  useStoredDocRunId,
} from "./docRunStore";

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
const NO_LOOPS = new Map<string, DocLoopProgress>();
const NO_LABELS = new Set<string>();
type Projection = {
  blocks: Map<string, WorkflowRunBlock>;
  loops: Map<string, DocLoopProgress>;
};
// Every step calls useDocRun; walk the timeline once per poll, not once per step.
const walked = new WeakMap<object, WeakMap<object, Projection>>();
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
  let projection = byRun.get(runKey);
  if (!projection) {
    const attempt = filterTimelineToAttempt(
      timeline,
      run?.attempts ?? [],
      getRunAttempt(run ?? {}),
    );
    projection = { blocks: new Map(), loops: new Map() };
    collectBlocks(attempt, projection.blocks, workflowRunId);
    collectLoopProgress(attempt, workflowRunId, projection.loops);
    byRun.set(runKey, projection);
  }
  return projection;
}

export const docLatestRunKey = (
  orgId: string | undefined,
  workflowPermanentId: string | undefined,
) =>
  getOrgScopedQueryKey(
    ["workflowRuns", "docLatest", workflowPermanentId],
    getActiveOrgQueryKeyScope(orgId),
  );

// Polls only while the tab is visible (react-query pauses interval refetches in a hidden tab).
const LATEST_RUN_POLL_MS = 15_000;
const LATEST_RUN_PAGE_SIZE = 20;
const MAX_LATEST_RUN_PAGES = 5;

// The agent's newest run from any browser or origin; every browser and tab reads the same one.
function useLatestRun(workflowPermanentId: string | undefined) {
  const credentialGetter = useCredentialGetter();
  const orgId = useActiveOrgId();
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: docLatestRunKey(orgId, workflowPermanentId),
    refetchInterval: LATEST_RUN_POLL_MS,
    refetchOnWindowFocus: "always",
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const fetchPage = async (page: number) =>
        (
          await client.get<
            Array<WorkflowRunApiResponse & { debug_session_id?: string | null }>
          >(`/workflows/${workflowPermanentId}/runs`, {
            params: { page, page_size: LATEST_RUN_PAGE_SIZE },
          })
        ).data;
      // A one-step (Play) run carries a debug session; it says nothing about the steps it did not run.
      const fullRuns = (runs: Awaited<ReturnType<typeof fetchPage>>) =>
        runs
          .filter((candidate) => !candidate.debug_session_id)
          .map((candidate) => candidate.workflow_run_id);
      let page = 1;
      let runs = await fetchPage(page);
      let ids = fullRuns(runs);
      // A full run on the first page is the answer. Without one, page back only while the query has no answer yet;
      // later polls keep that answer until the first page shows a newer full run.
      const earlier = queryClient.getQueryData<string[]>(
        docLatestRunKey(orgId, workflowPermanentId),
      );
      if (ids.length === 0 && earlier) {
        ids = earlier;
      } else if (ids.length === 0) {
        while (
          ids.length === 0 &&
          runs.length === LATEST_RUN_PAGE_SIZE &&
          page < MAX_LATEST_RUN_PAGES
        ) {
          runs = await fetchPage(++page);
          ids = fullRuns(runs);
        }
      }
      return ids;
    },
    enabled: Boolean(workflowPermanentId),
  });
}

/**
 * The studio's inspected run, or else the agent's latest run, projected onto
 * the Doc: the latest block row per step label (a looped step keeps its last
 * iteration), each loop's per-item progress, and whether the run is live.
 */
export function useDocRun() {
  const urlRunId = useStudioRunId();
  const orgId = useActiveOrgId();
  const workflowPermanentId = useWorkflowPermanentId();
  const storedRunId = useStoredDocRunId(orgId, workflowPermanentId);
  const dismissedRunId = useDismissedDocRunId(orgId, workflowPermanentId);
  const latest = useLatestRun(workflowPermanentId);
  // A run started here is written into the query, so the newest run always wins, wherever it was started.
  // Step (Play) runs started in this browser are skipped; the runs list marks no others (SKY-19814).
  const listed =
    latest.data === undefined
      ? storedRunId
      : (latest.data.find((id) => !isStepRun(id)) ?? null);
  const sharedRunId = listed === dismissedRunId ? null : listed;
  // This browser's own run stays on screen until it ends, even if a newer run starts elsewhere.
  const { data: storedRun } = useWorkflowRunWithWorkflowQuery({
    workflowRunId: storedRunId ?? undefined,
  });
  const pinnedRunId =
    storedRunId && storedRun && runIsLogicallyActive(storedRun)
      ? storedRunId
      : null;
  const runId = urlRunId ?? pinnedRunId ?? sharedRunId ?? undefined;
  // A one-step run (?bl=) says nothing about the steps it was never meant to run.
  const [searchParams] = useSearchParams();
  const blockRun = Boolean(urlRunId && searchParams.has("bl"));
  useEffect(() => {
    if (urlRunId && blockRun) markStepRun(urlRunId);
  }, [urlRunId, blockRun]);
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
  const { data: run, error } = useWorkflowRunWithWorkflowQuery({
    workflowRunId: runId ?? undefined,
  });
  // A remembered run that is gone (deleted, another org's) is forgotten, not retried.
  const storedGone =
    !urlRunId &&
    Boolean(sharedRunId) &&
    error instanceof AxiosError &&
    error.response?.status === 404;
  useEffect(() => {
    if (storedGone && workflowPermanentId)
      useDocRunStore.getState().setDocRunId(orgId, workflowPermanentId, null);
  }, [storedGone, orgId, workflowPermanentId]);
  const projection = useMemo(
    () => (timeline && runId ? blocksFor(timeline, run, runId) : null),
    [timeline, run, runId],
  );
  const { data: current } = useWorkflowQuery({ workflowPermanentId });
  // Accepting a tested proposal saves a newer version with the same steps; its test run still speaks for it.
  const earlierVersion = Boolean(
    run &&
    current &&
    run.workflow.version < current.version &&
    !sameSteps(run.workflow.workflow_definition, current.workflow_definition),
  );
  const unknownLabels = useMemo(
    () =>
      run && current && earlierVersion
        ? stepsUnknownToRun(
            run.workflow.workflow_definition,
            current.workflow_definition,
          )
        : NO_LABELS,
    [run, current, earlierVersion],
  );
  // A step added since the run (even unsaved) was not in it, so it cannot be "not reached".
  const ranLabels = useMemo(
    () => (run ? stepLabelsOf(run.workflow.workflow_definition) : null),
    [run],
  );
  return {
    ranLabels,
    earlierVersion,
    unknownLabels,
    sharedRunId: storedGone ? null : sharedRunId,
    runId: storedGone ? undefined : runId,
    run: storedGone ? undefined : run,
    blocksByLabel: projection?.blocks ?? EMPTY,
    loopsByLabel: projection?.loops ?? NO_LOOPS,
    live: Boolean(run && runIsLogicallyActive(run)),
    // Until the run's step rows arrive, a missing row means "not loaded", not "not reached".
    rowsLoaded: Boolean(projection),
    // Whether this browser started the run (or opened it by URL). Only an owned run locks the Doc,
    // offers Stop and ends with a toast; a run from elsewhere is drawn the same but read-only.
    owned: Boolean(urlRunId || (runId && runId === storedRunId)),
    blockRun,
  };
}
