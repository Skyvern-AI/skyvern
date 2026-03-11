import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import type { FallbackEpisodeListResponse } from "@/routes/workflows/types/scriptTypes";

function useFallbackEpisodesQuery({
  workflowPermanentId,
  workflowRunId,
  enabled = true,
}: {
  workflowPermanentId: string | undefined;
  workflowRunId: string | undefined;
  enabled?: boolean;
}) {
  const credentialGetter = useCredentialGetter();

  return useQuery<FallbackEpisodeListResponse>({
    queryKey: ["fallback-episodes", workflowPermanentId, workflowRunId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get(`/workflows/${workflowPermanentId}/fallback-episodes`, {
          params: {
            workflow_run_id: workflowRunId,
            page: 1,
            page_size: 100,
          },
        })
        .then((response) => response.data);
    },
    enabled: !!workflowPermanentId && !!workflowRunId && enabled,
    staleTime: Infinity,
  });
}

export { useFallbackEpisodesQuery };
