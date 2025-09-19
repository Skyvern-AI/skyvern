import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { WorkflowApiResponse } from "../types/workflowTypes";

type Props = {
  workflowPermanentId?: string;
};

export type WorkflowVersion = WorkflowApiResponse;

function useWorkflowVersionsQuery({ workflowPermanentId }: Props) {
  const credentialGetter = useCredentialGetter();

  return useQuery<WorkflowVersion[]>({
    queryKey: ["workflowVersions", workflowPermanentId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`/workflows/${workflowPermanentId}/versions`)
        .then((response) => response.data);
    },
    enabled: !!workflowPermanentId,
  });
}

export { useWorkflowVersionsQuery };
