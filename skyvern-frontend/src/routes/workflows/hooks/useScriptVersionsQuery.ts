import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { ScriptVersionListResponse } from "../types/scriptTypes";

type Props = {
  scriptId?: string | null;
};

function useScriptVersionsQuery({ scriptId }: Props) {
  const credentialGetter = useCredentialGetter();

  return useQuery<ScriptVersionListResponse>({
    queryKey: ["script-versions", scriptId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get(`/scripts/${scriptId}/versions`)
        .then((response) => response.data);
    },
    enabled: !!scriptId,
  });
}

export { useScriptVersionsQuery };
