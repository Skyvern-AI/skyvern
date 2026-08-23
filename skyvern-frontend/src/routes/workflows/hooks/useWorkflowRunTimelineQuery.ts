import { getClient } from "@/api/AxiosClient";
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

function useWorkflowRunTimelineQuery(options?: { workflowRunId?: string }) {
  const urlWorkflowRunId = useFirstParam("workflowRunId", "runId");
  const workflowRunId = options?.workflowRunId ?? urlWorkflowRunId;
  const credentialGetter = useCredentialGetter();
  const activeOrgId = useActiveOrgId();
  const activeOrgQueryKeyScope = getActiveOrgQueryKeyScope(activeOrgId);
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery(options);
  const workflow = workflowRun?.workflow;
  const workflowPermanentId = workflow?.workflow_permanent_id;

  return useQuery<Array<WorkflowRunTimelineItem>>({
    queryKey: getOrgScopedQueryKey(
      ["workflowRunTimeline", workflowPermanentId, workflowRunId],
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
        `/workflows/${workflowPermanentId}/runs/${workflowRunId}/timeline`,
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
      return workflowRun && statusIsNotFinalized(workflowRun)
        ? RUNNING_TIMELINE_REFETCH_INTERVAL_MS
        : false;
    },
    placeholderData: keepPreviousData,
    refetchOnMount:
      workflowRun && statusIsNotFinalized(workflowRun) ? "always" : false,
    refetchOnWindowFocus:
      workflowRun && statusIsNotFinalized(workflowRun) ? "always" : false,
    enabled: !!globalWorkflows && !!workflowPermanentId && !!workflowRunId,
  });
}

export { useWorkflowRunTimelineQuery };
