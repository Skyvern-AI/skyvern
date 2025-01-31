import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { statusIsNotFinalized } from "@/routes/tasks/types";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { WorkflowRunTimelineItem } from "../types/workflowRunTypes";
import { useWorkflowRunQuery } from "./useWorkflowRunQuery";
import { useGlobalWorkflowsQuery } from "./useGlobalWorkflowsQuery";

function useWorkflowRunTimelineQuery() {
  const { workflowRunId, workflowPermanentId } = useParams();
  const credentialGetter = useCredentialGetter();
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const { data: workflowRun } = useWorkflowRunQuery();

  return useQuery<Array<WorkflowRunTimelineItem>>({
    queryKey: ["workflowRunTimeline", workflowPermanentId, workflowRunId],
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
        .get(
          `/workflows/${workflowPermanentId}/runs/${workflowRunId}/timeline`,
          { params },
        )
        .then((response) => response.data);
    },
    refetchInterval:
      workflowRun && statusIsNotFinalized(workflowRun) ? 5000 : false,
    placeholderData: keepPreviousData,
    refetchOnMount:
      workflowRun && statusIsNotFinalized(workflowRun) ? "always" : false,
    refetchOnWindowFocus:
      workflowRun && statusIsNotFinalized(workflowRun) ? "always" : false,
    enabled: !!globalWorkflows && !!workflowPermanentId && !!workflowRunId,
  });
}

export { useWorkflowRunTimelineQuery };
