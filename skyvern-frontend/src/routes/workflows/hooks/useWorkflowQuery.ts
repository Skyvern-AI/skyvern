import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { WorkflowApiResponse } from "../types/workflowTypes";
import { globalWorkflowIds } from "@/util/env";

type Props = {
  workflowPermanentId?: string;
};

function useWorkflowQuery({ workflowPermanentId }: Props) {
  const credentialGetter = useCredentialGetter();
  return useQuery<WorkflowApiResponse>({
    queryKey: ["workflow", workflowPermanentId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const isGlobalWorkflow =
        workflowPermanentId && globalWorkflowIds.includes(workflowPermanentId);
      const params = new URLSearchParams();
      if (isGlobalWorkflow) {
        params.set("template", "true");
      }
      return client
        .get(`/workflows/${workflowPermanentId}`, { params })
        .then((response) => response.data);
    },
  });
}

export { useWorkflowQuery };
