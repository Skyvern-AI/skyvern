import { getClient } from "@/api/AxiosClient";
import { WorkflowRunStatusApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  statusIsNotFinalized,
  statusIsRunningOrQueued,
} from "@/routes/tasks/types";
import { globalWorkflowIds } from "@/util/env";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

function useWorkflowRunQuery() {
  const { workflowRunId, workflowPermanentId } = useParams();
  const credentialGetter = useCredentialGetter();

  return useQuery<WorkflowRunStatusApiResponse>({
    queryKey: ["workflowRun", workflowPermanentId, workflowRunId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const isGlobalWorkflow =
        workflowPermanentId && globalWorkflowIds.includes(workflowPermanentId);
      const params = new URLSearchParams();
      if (isGlobalWorkflow) {
        params.set("template", "true");
      }
      return client
        .get(`/workflows/${workflowPermanentId}/runs/${workflowRunId}`, {
          params,
        })
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
  });
}

export { useWorkflowRunQuery };
