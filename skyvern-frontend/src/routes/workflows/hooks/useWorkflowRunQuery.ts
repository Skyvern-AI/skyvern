import { getClient } from "@/api/AxiosClient";
import { Status, WorkflowRunStatusApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useFirstParam } from "@/hooks/useFirstParam";
import {
  statusIsNotFinalized,
  statusIsRunningOrQueued,
} from "@/routes/tasks/types";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";
import { useGlobalWorkflowsQuery } from "./useGlobalWorkflowsQuery";
import {
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";

const RUN_STATUS_POLL_INTERVAL_MS = 5000;
// Generous against the poll interval because the gap is measured from the last success, and sleep,
// hidden-tab timer throttling, and retry backoff all widen that gap before an outage even starts.
const POLL_OUTAGE_BUDGET_MS = 120000;
const RUN_STATUS_OUTAGE_RETRY_INTERVAL_MS = 30000;

// Data from before a failed refetch is retained. Poll frequently through a
// short outage, then retry at a quieter cadence until the run status resolves.
// This keeps reconciliation automatic without retrying a persistent error every
// five seconds. The outage is measured from the last success rather than from
// fetchFailureCount, which query-core resets at the start of every fetch.
function getRunStatusRefetchInterval(state: {
  status: "pending" | "error" | "success";
  data?: { status: Status };
  dataUpdatedAt: number;
  errorUpdatedAt: number;
}): number | false {
  if (!state.data) {
    return false;
  }
  if (!statusIsNotFinalized(state.data)) {
    return false;
  }
  if (
    state.status === "error" &&
    state.errorUpdatedAt - state.dataUpdatedAt > POLL_OUTAGE_BUDGET_MS
  ) {
    return RUN_STATUS_OUTAGE_RETRY_INTERVAL_MS;
  }
  return RUN_STATUS_POLL_INTERVAL_MS;
}

function useWorkflowRunQuery(options?: {
  workflowRunId?: string;
  enabled?: boolean;
}) {
  const urlWorkflowRunId = useFirstParam("workflowRunId", "runId");
  const workflowRunId = options?.workflowRunId ?? urlWorkflowRunId;
  const workflowPermanentId = useWorkflowPermanentId();
  const credentialGetter = useCredentialGetter();
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const activeOrgId = useActiveOrgId();
  const activeOrgQueryKeyScope = getActiveOrgQueryKeyScope(activeOrgId);

  return useQuery<WorkflowRunStatusApiResponse>({
    queryKey: getOrgScopedQueryKey(
      ["workflowRun", workflowPermanentId, workflowRunId],
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
      return client
        .get(`/workflows/${workflowPermanentId}/runs/${workflowRunId}`, {
          params,
          signal,
        })
        .then((response) => response.data);
    },
    refetchInterval: (query) => getRunStatusRefetchInterval(query.state),
    // required for OS-level notifications to work (workflow run completion)
    refetchIntervalInBackground: true,
    placeholderData: keepPreviousData,
    refetchOnMount: (query) => {
      if (!query.state.data) {
        return false;
      }
      return statusIsRunningOrQueued(query.state.data) ? "always" : false;
    },
    refetchOnWindowFocus: (query) => {
      if (!query.state.data) {
        return false;
      }
      return statusIsRunningOrQueued(query.state.data);
    },
    enabled:
      (options?.enabled ?? true) &&
      !!globalWorkflows &&
      !!workflowPermanentId &&
      !!workflowRunId,
  });
}

export {
  getRunStatusRefetchInterval,
  POLL_OUTAGE_BUDGET_MS,
  RUN_STATUS_OUTAGE_RETRY_INTERVAL_MS,
  RUN_STATUS_POLL_INTERVAL_MS,
  useWorkflowRunQuery,
};
