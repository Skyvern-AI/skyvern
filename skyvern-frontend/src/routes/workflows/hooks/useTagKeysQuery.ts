import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useWorkflowTaggingEnabled } from "@/hooks/useWorkflowTaggingEnabled";
import { useQuery } from "@tanstack/react-query";
import type { TagKey } from "../types/tagTypes";

function useTagKeysQuery({ enabled = true }: { enabled?: boolean } = {}) {
  const credentialGetter = useCredentialGetter();
  const taggingEnabled = useWorkflowTaggingEnabled();

  return useQuery({
    queryKey: ["tag-keys"],
    enabled: enabled && taggingEnabled,
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get<Array<TagKey>>("/tag-keys")
        .then((response) => response.data);
    },
  });
}

export { useTagKeysQuery };
