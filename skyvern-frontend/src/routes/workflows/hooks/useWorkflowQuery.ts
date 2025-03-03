import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { WorkflowApiResponse } from "../types/workflowTypes";
import { useGlobalWorkflowsQuery } from "./useGlobalWorkflowsQuery";

type Props = {
  workflowPermanentId?: string;
};

function useWorkflowQuery({ workflowPermanentId }: Props) {
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const credentialGetter = useCredentialGetter();

  return useQuery<WorkflowApiResponse>({
    queryKey: ["workflow", workflowPermanentId],
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
        .get(`/workflows/${workflowPermanentId}`, { params })
        .then((response) => response.data);
    },
    enabled: !!globalWorkflows && !!workflowPermanentId,
  });
}

export { useWorkflowQuery };
