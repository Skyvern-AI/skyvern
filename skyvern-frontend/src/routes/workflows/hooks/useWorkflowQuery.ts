import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { WorkflowApiResponse } from "../types/workflowTypes";

type Props = {
  workflowPermanentId?: string;
};

function useWorkflowQuery({ workflowPermanentId }: Props) {
  const credentialGetter = useCredentialGetter();
  return useQuery<WorkflowApiResponse>({
    queryKey: ["workflow", workflowPermanentId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`/workflows/${workflowPermanentId}`)
        .then((response) => response.data);
    },
  });
}

export { useWorkflowQuery };
