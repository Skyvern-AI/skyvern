import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { ScriptVersionDetailResponse } from "../types/scriptTypes";

type Props = {
  scriptId?: string | null;
  version?: number | null;
};

function useScriptVersionDetailQuery({ scriptId, version }: Props) {
  const credentialGetter = useCredentialGetter();

  return useQuery<ScriptVersionDetailResponse>({
    queryKey: ["script-version-detail", scriptId, version],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get(`/scripts/${scriptId}/versions/${version}/detail`)
        .then((response) => response.data);
    },
    enabled: !!scriptId && version != null,
  });
}

export { useScriptVersionDetailQuery };
