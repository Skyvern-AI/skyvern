import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { ScriptVersionCompareResponse } from "../types/scriptTypes";

type Props = {
  scriptId?: string | null;
  baseVersion?: number | null;
  compareVersion?: number | null;
};

function useScriptVersionCompareQuery({
  scriptId,
  baseVersion,
  compareVersion,
}: Props) {
  const credentialGetter = useCredentialGetter();

  return useQuery<ScriptVersionCompareResponse>({
    queryKey: ["script-version-compare", scriptId, baseVersion, compareVersion],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get(`/scripts/${scriptId}/compare`, {
          params: { base: baseVersion, compare: compareVersion },
        })
        .then((response) => response.data);
    },
    enabled: !!scriptId && baseVersion != null && compareVersion != null,
  });
}

export { useScriptVersionCompareQuery };
