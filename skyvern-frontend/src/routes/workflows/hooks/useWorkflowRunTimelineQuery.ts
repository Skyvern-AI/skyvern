import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { statusIsNotFinalized } from "@/routes/tasks/types";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { WorkflowRunTimelineItem } from "../types/workflowRunTypes";
import { useWorkflowRunQuery } from "./useWorkflowRunQuery";

function useWorkflowRunTimelineQuery() {
  const { workflowRunId, workflowPermanentId } = useParams();
  const credentialGetter = useCredentialGetter();
  const { data: workflowRun } = useWorkflowRunQuery();

  return useQuery<Array<WorkflowRunTimelineItem>>({
    queryKey: ["workflowRunTimeline", workflowPermanentId, workflowRunId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`/workflows/${workflowPermanentId}/runs/${workflowRunId}/timeline`)
        .then((response) => response.data);
    },
    refetchInterval:
      workflowRun && statusIsNotFinalized(workflowRun) ? 5000 : false,
    placeholderData: keepPreviousData,
    refetchOnMount:
      workflowRun && statusIsNotFinalized(workflowRun) ? "always" : false,
    refetchOnWindowFocus:
      workflowRun && statusIsNotFinalized(workflowRun) ? "always" : false,
  });
}

export { useWorkflowRunTimelineQuery };
