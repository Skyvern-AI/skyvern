import { useCallback } from "react";
import { getClient } from "@/api/AxiosClient";
import { Status, WorkflowRunStatusApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useFirstParam } from "@/hooks/useFirstParam";
import {
  statusIsNotFinalized,
  statusIsRunningOrQueued,
} from "@/routes/tasks/types";
import {
  DefaultError,
  keepPreviousData,
  useQuery,
} from "@tanstack/react-query";
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

// The key is required so that passing an options object always states a run, even
// when that run is undefined; omitting the object entirely is what defers to the
// route. Optional-key typing made those two cases identical to tsc.
function useWorkflowRunQuery(options?: { workflowRunId: string | undefined }) {
  const urlWorkflowRunId = useFirstParam("workflowRunId", "runId");
  const workflowRunId = options ? options.workflowRunId : urlWorkflowRunId;
  const workflowPermanentId = useWorkflowPermanentId();
  const credentialGetter = useCredentialGetter();
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const activeOrgId = useActiveOrgId();
  const activeOrgQueryKeyScope = getActiveOrgQueryKeyScope(activeOrgId);
  // A fresh arrow each render defeats query-core's select memo, re-running the
  // comparison and a deep equality check over the whole payload every time.
  const selectRequestedRun = useCallback(
    (run: WorkflowRunStatusApiResponse) =>
      run.workflow_run_id === workflowRunId ? run : undefined,
    [workflowRunId],
  );

  return useQuery<
    WorkflowRunStatusApiResponse,
    DefaultError,
    WorkflowRunStatusApiResponse | undefined
  >({
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
        .get(
          `/workflows/${workflowPermanentId}/runs/${encodeURIComponent(workflowRunId ?? "")}`,
          {
            params,
            signal,
          },
        )
        .then((response) => response.data);
    },
    refetchInterval: (query) => getRunStatusRefetchInterval(query.state),
    // required for OS-level notifications to work (workflow run completion)
    refetchIntervalInBackground: true,
    placeholderData: keepPreviousData,
    // keepPreviousData serves the previous run's payload whenever the requested run
    // changes or clears, so withhold it rather than present it as the run in view.
    select: selectRequestedRun,
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
    enabled: !!globalWorkflows && !!workflowPermanentId && !!workflowRunId,
  });
}

export {
  getRunStatusRefetchInterval,
  POLL_OUTAGE_BUDGET_MS,
  RUN_STATUS_OUTAGE_RETRY_INTERVAL_MS,
  RUN_STATUS_POLL_INTERVAL_MS,
  useWorkflowRunQuery,
};
