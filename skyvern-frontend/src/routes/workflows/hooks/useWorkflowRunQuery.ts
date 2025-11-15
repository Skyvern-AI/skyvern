import { getClient } from "@/api/AxiosClient";
import { WorkflowRunStatusApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useFirstParam } from "@/hooks/useFirstParam";
import {
  statusIsNotFinalized,
  statusIsRunningOrQueued,
} from "@/routes/tasks/types";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { useGlobalWorkflowsQuery } from "./useGlobalWorkflowsQuery";

function useWorkflowRunQuery() {
  const workflowRunId = useFirstParam("workflowRunId", "runId");
  const { workflowPermanentId } = useParams();
  const credentialGetter = useCredentialGetter();
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();

  return useQuery<WorkflowRunStatusApiResponse>({
    queryKey: ["workflowRun", workflowPermanentId, workflowRunId],
    queryFn: async () => {
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
    enabled: !!globalWorkflows && !!workflowPermanentId && !!workflowRunId,
  });
}

export { useWorkflowRunQuery };
