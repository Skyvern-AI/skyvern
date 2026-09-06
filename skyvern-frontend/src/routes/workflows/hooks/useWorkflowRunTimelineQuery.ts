import { getClient } from "@/api/AxiosClient";
import { Status } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { statusIsNotFinalized } from "@/routes/tasks/types";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { WorkflowRunTimelineItem } from "../types/workflowRunTypes";
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
  const runIsLive = !!workflowRun && statusIsNotFinalized(workflowRun);
  // Only a running run writes timeline rows. Created, queued and paused are live but idle, and a
  // paused run waits on a human for as long as that takes.
  const runIsWriting = workflowRun?.status === Status.Running;

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
      return runIsLive ? RUNNING_TIMELINE_REFETCH_INTERVAL_MS : false;
    },
    // The interval otherwise pauses while the window is unfocused, and a run watched from another
    // window keeps writing blocks the whole time. Scoped to a running run so a backgrounded tab
    // does not poll through an idle one — a paused run can sit there for hours.
    refetchIntervalInBackground: runIsWriting,
    placeholderData: keepPreviousData,
    refetchOnMount: runIsLive ? "always" : false,
    refetchOnWindowFocus: runIsLive ? "always" : false,
    enabled: !!globalWorkflows && !!workflowPermanentId && !!workflowRunId,
  });
}

export { RUNNING_TIMELINE_REFETCH_INTERVAL_MS, useWorkflowRunTimelineQuery };
