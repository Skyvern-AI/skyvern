import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import type { TagKey } from "../types/tagTypes";

function useTagKeysQuery({ enabled = true }: { enabled?: boolean } = {}) {
  const credentialGetter = useCredentialGetter();

  return useQuery({
    queryKey: ["tag-keys"],
    enabled,
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get<Array<TagKey>>("/tag-keys")
        .then((response) => response.data);
    },
  });
}

export { useTagKeysQuery };
