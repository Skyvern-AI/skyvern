import { useEffect, useRef } from "react";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { statusIsNotFinalized } from "@/routes/tasks/types";
import {
  keepPreviousData,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { WorkflowRunTimelineItem } from "../types/workflowRunTypes";
import { useWorkflowRunWithWorkflowQuery } from "./useWorkflowRunWithWorkflowQuery";
import { useGlobalWorkflowsQuery } from "./useGlobalWorkflowsQuery";
import { useFirstParam } from "@/hooks/useFirstParam";

function useWorkflowRunTimelineQuery() {
  const workflowRunId = useFirstParam("workflowRunId", "runId");
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const { data: workflowRun, dataUpdatedAt } =
    useWorkflowRunWithWorkflowQuery();
  const workflow = workflowRun?.workflow;
  const workflowPermanentId = workflow?.workflow_permanent_id;

  // Track when workflow run data was last updated
  const prevDataUpdatedAtRef = useRef<number>(dataUpdatedAt);

  // Refetch timeline whenever the workflow run query gets new data.
  // This keeps the timeline perfectly synchronized with the workflow run status,
  // ensuring we never miss updates (e.g., when workflow completes).
  useEffect(() => {
    if (
      dataUpdatedAt !== prevDataUpdatedAtRef.current &&
      workflowPermanentId &&
      workflowRunId
    ) {
      queryClient.invalidateQueries({
        queryKey: ["workflowRunTimeline", workflowPermanentId, workflowRunId],
      });
    }
    prevDataUpdatedAtRef.current = dataUpdatedAt;
  }, [dataUpdatedAt, workflowPermanentId, workflowRunId, queryClient]);

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
    // No independent refetchInterval - timeline follows workflow run query's timing
    // via the useEffect above that invalidates on dataUpdatedAt changes
    placeholderData: keepPreviousData,
    refetchOnMount:
      workflowRun && statusIsNotFinalized(workflowRun) ? "always" : false,
    refetchOnWindowFocus:
      workflowRun && statusIsNotFinalized(workflowRun) ? "always" : false,
    enabled: !!globalWorkflows && !!workflowPermanentId && !!workflowRunId,
  });
}

export { useWorkflowRunTimelineQuery };
