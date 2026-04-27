import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { WorkflowScriptsListResponse } from "../types/scriptTypes";

type Props = {
  workflowPermanentId?: string;
};

function useWorkflowScriptsQuery({ workflowPermanentId }: Props) {
  const credentialGetter = useCredentialGetter();

  return useQuery<WorkflowScriptsListResponse>({
    queryKey: ["workflow-scripts", workflowPermanentId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get(`/scripts/workflows/${workflowPermanentId}`)
        .then((response) => response.data);
    },
    enabled: !!workflowPermanentId,
    staleTime: 5 * 60 * 1000,
  });
}

export { useWorkflowScriptsQuery };
