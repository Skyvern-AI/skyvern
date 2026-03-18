import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import type { ScriptRunsResponse } from "../types/scriptTypes";

type Props = {
  scriptId?: string;
  pageSize?: number;
};

function useScriptRunsQuery({ scriptId, pageSize = 50 }: Props) {
  const credentialGetter = useCredentialGetter();

  return useQuery<ScriptRunsResponse>({
    queryKey: ["script-runs", scriptId, pageSize],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get(`/scripts/${scriptId}/runs?page_size=${pageSize}`)
        .then((r) => r.data);
    },
    enabled: !!scriptId,
  });
}

export { useScriptRunsQuery };
