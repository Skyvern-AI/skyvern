import {
  runIsLogicallyActive,
  runIsLogicallyFinal,
} from "@/routes/workflows/workflowRun/runRetryState";
import { statusIsNotFinalized } from "@/routes/tasks/types";
import { getClient } from "@/api/AxiosClient";
import { Status } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  isBlockItem,
  WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";
import { useWorkflowRunWithWorkflowQuery } from "./useWorkflowRunWithWorkflowQuery";
import { useGlobalWorkflowsQuery } from "./useGlobalWorkflowsQuery";
import { useFirstParam } from "@/hooks/useFirstParam";
import {
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";

// Mirrors RECORDED_ACTIONS_POLL_INTERVAL_MS in WorkflowCopilotChat, the other reader of
// this endpoint; the two must not drift apart or their counts disagree on screen.
const RUNNING_TIMELINE_REFETCH_INTERVAL_MS = 2500;
// A final run's blocks, whatever its final status, can close after the run itself turns final.
// The cap keeps a worker that never closes them from polling forever.
const POST_FINAL_TIMELINE_POLL_CAP = 20;

function timelineHasOpenBlock(items: Array<WorkflowRunTimelineItem>): boolean {
  return items.some(
    (item) =>
      (isBlockItem(item) &&
        [item.block, ...(item.task_v2_blocks ?? [])].some(
          ({ status }) => status !== null && statusIsNotFinalized({ status }),
        )) ||
      timelineHasOpenBlock(item.children),
  );
}

function useWorkflowRunTimelineQuery(options?: {
  workflowRunId: string | undefined;
}) {
  const urlWorkflowRunId = useFirstParam("workflowRunId", "runId");
  // Same caller-intent rule as the run hooks this forwards to; `??` here would
  // resolve a cleared caller id back to the route's run.
  const workflowRunId = options ? options.workflowRunId : urlWorkflowRunId;
  const credentialGetter = useCredentialGetter();
  const activeOrgId = useActiveOrgId();
  const activeOrgQueryKeyScope = getActiveOrgQueryKeyScope(activeOrgId);
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery(options);
  const workflow = workflowRun?.workflow;
  const workflowPermanentId = workflow?.workflow_permanent_id;
  const runIsLive = !!workflowRun && runIsLogicallyActive(workflowRun);
  // Only a running run writes timeline rows. Created, queued and paused are live but idle, and a
  // paused run waits on a human for as long as that takes.
  const runIsWriting = workflowRun?.status === Status.Running;
  const runIsFinal = !!workflowRun && runIsLogicallyFinal(workflowRun);

  return useQuery<Array<WorkflowRunTimelineItem>>({
    // The run's status is part of what was read: a timeline fetched while the run was still
    // writing is a different, incomplete answer than one fetched after it stopped. Keying on it
    // makes reaching a terminal state re-read exactly once — the poll cannot be relied on for
    // that, because the run-status query can report the terminal state between two ticks and
    // cancel the timer before it fires again. keepPreviousData holds the last rows on screen
    // across the swap.
    queryKey: getOrgScopedQueryKey(
      [
        "workflowRunTimeline",
        workflowPermanentId,
        workflowRunId,
        workflowRun?.status,
        workflowRun?.attempt,
        workflowRun?.retry_pending,
      ],
      activeOrgQueryKeyScope,
    ),
    queryFn: async ({ signal }) => {
      const client = await getClient(credentialGetter);
      const isGlobalWorkflow = globalWorkflows?.some(
        (workflow) => workflow.workflow_permanent_id === workflowPermanentId,
      );
      const params = new URLSearchParams();
      if (isGlobalWorkflow) {
        params.set("template", "true");
      }
      const { data } = await client.get(
        `/workflows/${workflowPermanentId}/runs/${encodeURIComponent(workflowRunId ?? "")}/timeline`,
        { params, signal },
      );
      // axios hands back the raw string when a 2xx body fails JSON.parse, so an
      // unvalidated response.data reaches callers typed as an array but isn't one.
      if (!Array.isArray(data)) {
        throw new Error("Workflow run timeline response was not an array");
      }
      return data;
    },
    // The header counted off this data used to refetch only when the 5s run query landed,
    // so it trailed the copilot chat's own 2.5s timeline poll and could report no actions
    // while rows were visibly executing. Polling directly replaces that chained refetch.
    // The gate reads the run query's cached data, which is retained after failed
    // refetches, so this query's own error state has to stop the poll.
    refetchInterval: (query) => {
      if (query.state.status === "error") {
        return false;
      }
      if (runIsWriting) {
        return RUNNING_TIMELINE_REFETCH_INTERVAL_MS;
      }
      return runIsFinal &&
        query.state.dataUpdateCount < POST_FINAL_TIMELINE_POLL_CAP &&
        timelineHasOpenBlock(query.state.data ?? [])
        ? RUNNING_TIMELINE_REFETCH_INTERVAL_MS
        : false;
    },
    // The interval otherwise pauses while the window is unfocused, and a run watched from another
    // window keeps writing blocks the whole time. Scoped to a running run, or a final one closing
    // its last blocks under the cap, so a backgrounded tab does not poll through a paused run for
    // hours.
    refetchIntervalInBackground: runIsWriting || runIsFinal,
    placeholderData: keepPreviousData,
    // A final run whose cached rows are still open may have stopped at the poll cap, and staleTime
    // would otherwise keep those rows on screen for every later visit.
    refetchOnMount: (query) =>
      runIsLive || (runIsFinal && timelineHasOpenBlock(query.state.data ?? []))
        ? "always"
        : false,
    refetchOnWindowFocus: runIsLive ? "always" : false,
    enabled: !!globalWorkflows && !!workflowPermanentId && !!workflowRunId,
  });
}

export {
  POST_FINAL_TIMELINE_POLL_CAP,
  RUNNING_TIMELINE_REFETCH_INTERVAL_MS,
  useWorkflowRunTimelineQuery,
};
