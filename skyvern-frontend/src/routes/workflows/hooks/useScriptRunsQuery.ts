import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import type { ScriptRunsResponse } from "../types/scriptTypes";

type Props = {
  scriptId?: string;
  pageSize?: number;
  version?: number;
};

function useScriptRunsQuery({ scriptId, pageSize = 50, version }: Props) {
  const credentialGetter = useCredentialGetter();

  return useQuery<ScriptRunsResponse>({
    queryKey: ["script-runs", scriptId, pageSize, version],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const params = new URLSearchParams({ page_size: String(pageSize) });
      if (version !== undefined) {
        params.set("version", String(version));
      }
      return client
        .get(`/scripts/${scriptId}/runs?${params}`)
        .then((r) => r.data);
    },
    enabled: !!scriptId,
  });
}

export { useScriptRunsQuery };
