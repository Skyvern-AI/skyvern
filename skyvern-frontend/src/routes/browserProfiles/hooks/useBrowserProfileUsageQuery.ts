import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { BrowserProfileUsage } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

type UseQueryOptions = Omit<
  Parameters<typeof useQuery<BrowserProfileUsage>>[0],
  "queryKey" | "queryFn"
>;

function useBrowserProfileUsageQuery(
  profileId: string | undefined,
  options: UseQueryOptions = {},
) {
  const credentialGetter = useCredentialGetter();

  return useQuery<BrowserProfileUsage>({
    queryKey: ["browserProfileUsage", profileId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get<BrowserProfileUsage>(`/browser_profiles/${profileId}/usage`)
        .then((response) => response.data);
    },
    enabled: Boolean(profileId),
    staleTime: 60 * 1000,
    ...options,
  });
}

export { useBrowserProfileUsageQuery };
