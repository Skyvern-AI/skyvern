import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { BrowserProfile } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

function useBrowserProfileQuery(profileId: string | null | undefined) {
  const credentialGetter = useCredentialGetter();

  return useQuery<BrowserProfile | null>({
    queryKey: ["browserProfile", profileId],
    enabled: Boolean(profileId),
    queryFn: async () => {
      if (!profileId) {
        return null;
      }

      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get(`/browser_profiles/${profileId}`)
        .then((response) => response.data);
    },
  });
}

export { useBrowserProfileQuery };
