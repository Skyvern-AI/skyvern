import { useCallback } from "react";
import { getClient } from "@/api/AxiosClient";
import { WorkflowRunStatusApiResponseWithWorkflow } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import {
  DefaultError,
  keepPreviousData,
  useQuery,
} from "@tanstack/react-query";
import { useFirstParam } from "@/hooks/useFirstParam";
import { getRunStatusRefetchInterval } from "./useWorkflowRunQuery";
import {
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";

// The key is required so that passing an options object always states a run, even
// when that run is undefined; omitting the object entirely is what defers to the
// route. Optional-key typing made those two cases identical to tsc.
function useWorkflowRunWithWorkflowQuery(options?: {
  workflowRunId: string | undefined;
  enabled?: boolean;
}) {
  const urlWorkflowRunId = useFirstParam("workflowRunId", "runId");
  const workflowRunId = options ? options.workflowRunId : urlWorkflowRunId;
  const credentialGetter = useCredentialGetter();
  const activeOrgId = useActiveOrgId();
  const activeOrgQueryKeyScope = getActiveOrgQueryKeyScope(activeOrgId);
  // A fresh arrow each render defeats query-core's select memo, re-running the
  // comparison and a deep equality check over the whole payload every time.
  const selectRequestedRun = useCallback(
    (run: WorkflowRunStatusApiResponseWithWorkflow) =>
      run.workflow_run_id === workflowRunId ? run : undefined,
    [workflowRunId],
  );

  return useQuery<
    WorkflowRunStatusApiResponseWithWorkflow,
    DefaultError,
    WorkflowRunStatusApiResponseWithWorkflow | undefined
  >({
    queryKey: getOrgScopedQueryKey(
      ["workflowRun", workflowRunId],
      activeOrgQueryKeyScope,
    ),
    queryFn: async ({ signal }) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get(`/workflows/runs/${encodeURIComponent(workflowRunId ?? "")}`, {
          signal,
        })
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
    enabled: (options?.enabled ?? true) && !!workflowRunId,
  });
}

export { useWorkflowRunWithWorkflowQuery };
