import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { ScriptBlocksResponse } from "../types/scriptTypes";

type Props = {
  cacheKey?: string;
  cacheKeyValue?: string;
  workflowPermanentId?: string;
};

function useBlockScriptsQuery({
  cacheKey,
  cacheKeyValue,
  workflowPermanentId,
}: Props) {
  const credentialGetter = useCredentialGetter();

  return useQuery<{ [blockName: string]: string }>({
    queryKey: ["block-scripts", workflowPermanentId, cacheKey, cacheKeyValue],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");

      const result = await client
        .post<ScriptBlocksResponse>(`/scripts/${workflowPermanentId}/blocks`, {
          cache_key: cacheKey ?? "",
          cache_key_value: cacheKeyValue ?? "",
        })
        .then((response) => response.data);

      return result.blocks;
    },
    enabled: !!workflowPermanentId,
  });
}

export { useBlockScriptsQuery };
