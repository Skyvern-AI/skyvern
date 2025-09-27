import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { ScriptBlocksResponse } from "../types/scriptTypes";

type Props = {
  cacheKey?: string;
  cacheKeyValue?: string;
  workflowPermanentId?: string;
  pollIntervalMs?: number;
  status?: "pending" | "published";
  workflowRunId?: string;
};

function useBlockScriptsQuery({
  cacheKey,
  cacheKeyValue,
  workflowPermanentId,
  pollIntervalMs,
  status,
  workflowRunId,
}: Props) {
  const credentialGetter = useCredentialGetter();

  return useQuery<{ [blockName: string]: string }>({
    queryKey: [
      "block-scripts",
      workflowPermanentId,
      cacheKey,
      cacheKeyValue,
      status,
      workflowRunId,
    ],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");

      const result = await client
        .post<ScriptBlocksResponse>(`/scripts/${workflowPermanentId}/blocks`, {
          cache_key: cacheKey ?? "",
          cache_key_value: cacheKeyValue ?? "",
          status: status ?? "published",
          workflow_run_id: workflowRunId ?? null,
        })
        .then((response) => response.data);

      return result.blocks;
    },
    refetchInterval: () => {
      if (!pollIntervalMs || pollIntervalMs === 0) {
        return false;
      }
      return Math.max(2000, pollIntervalMs);
    },
    enabled: !!workflowPermanentId,
  });
}

export { useBlockScriptsQuery };
