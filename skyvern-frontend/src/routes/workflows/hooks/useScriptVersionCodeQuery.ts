import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { ScriptBlocksResponse } from "../types/scriptTypes";

type Props = {
  scriptId?: string | null;
  version?: number | null;
};

function useScriptVersionCodeQuery({ scriptId, version }: Props) {
  const credentialGetter = useCredentialGetter();

  return useQuery<ScriptBlocksResponse>({
    queryKey: ["script-version-code", scriptId, version],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get(`/scripts/${scriptId}/versions/${version}`)
        .then((response) => response.data);
    },
    enabled: !!scriptId && version != null,
  });
}

export { useScriptVersionCodeQuery };
