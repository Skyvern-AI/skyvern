import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { WorkflowApiResponse } from "../types/workflowTypes";

type UseActiveImportsQueryParams = {
  enabled?: boolean;
  refetchInterval?: number | false;
};

export function useActiveImportsQuery({
  enabled = true,
  refetchInterval = false,
}: UseActiveImportsQueryParams = {}) {
  const credentialGetter = useCredentialGetter();

  return useQuery({
    queryKey: ["active-imports"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.get<WorkflowApiResponse[]>("/workflows", {
        params: {
          status: ["importing", "import_failed"],
          page: 1,
          page_size: 20,
        },
        paramsSerializer: {
          indexes: null, // Remove brackets from array params: status=a&status=b instead of status[]=a&status[]=b
        },
      });
      return response.data;
    },
    enabled,
    refetchInterval,
    refetchIntervalInBackground: true,
    staleTime: 0, // Always consider data stale so it refetches immediately
  });
}
