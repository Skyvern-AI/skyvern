import { getClient } from "@/api/AxiosClient";
import { WorkflowRunStatusApiResponseWithWorkflow } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  statusIsNotFinalized,
  statusIsRunningOrQueued,
} from "@/routes/tasks/types";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useFirstParam } from "@/hooks/useFirstParam";
import {
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";

function useWorkflowRunWithWorkflowQuery(options?: {
  workflowRunId?: string;
  enabled?: boolean;
}) {
  const urlWorkflowRunId = useFirstParam("workflowRunId", "runId");
  const workflowRunId = options?.workflowRunId ?? urlWorkflowRunId;
  const credentialGetter = useCredentialGetter();
  const activeOrgId = useActiveOrgId();
  const activeOrgQueryKeyScope = getActiveOrgQueryKeyScope(activeOrgId);

  return useQuery<WorkflowRunStatusApiResponseWithWorkflow>({
    queryKey: getOrgScopedQueryKey(
      ["workflowRun", workflowRunId],
      activeOrgQueryKeyScope,
    ),
    queryFn: async ({ signal }) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get(`/workflows/runs/${workflowRunId}`, { signal })
        .then((response) => response.data);
    },
    refetchInterval: (query) => {
      if (!query.state.data) {
        return false;
      }
      if (statusIsNotFinalized(query.state.data)) {
        return 5000;
      }
      return false;
    },
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
    enabled: (options?.enabled ?? true) && !!workflowRunId,
  });
}

export { useWorkflowRunWithWorkflowQuery };
