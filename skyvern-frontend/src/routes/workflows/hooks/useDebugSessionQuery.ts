import { getClient } from "@/api/AxiosClient";
import { DebugSessionApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";

interface Opts {
  workflowPermanentId?: string;
  enabled?: boolean;
}

function useDebugSessionQuery({ workflowPermanentId, enabled }: Opts) {
  const credentialGetter = useCredentialGetter();

  return useQuery<DebugSessionApiResponse>({
    queryKey: ["debugSession", workflowPermanentId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get(`/debug-session/${workflowPermanentId}`)
        .then((response) => response.data);
    },
    enabled:
      enabled !== undefined
        ? enabled && !!workflowPermanentId
        : !!workflowPermanentId,
  });
}

export { useDebugSessionQuery };
