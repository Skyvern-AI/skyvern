import { getClient } from "@/api/AxiosClient";
import { WorkflowRunStatusApiResponseWithWorkflow } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  statusIsNotFinalized,
  statusIsRunningOrQueued,
} from "@/routes/tasks/types";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useFirstParam } from "@/hooks/useFirstParam";

function useWorkflowRunWithWorkflowQuery() {
  const workflowRunId = useFirstParam("workflowRunId", "runId");
  const credentialGetter = useCredentialGetter();

  return useQuery<WorkflowRunStatusApiResponseWithWorkflow>({
    queryKey: ["workflowRun", workflowRunId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get(`/workflows/runs/${workflowRunId}`)
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
    enabled: !!workflowRunId,
  });
}

export { useWorkflowRunWithWorkflowQuery };
