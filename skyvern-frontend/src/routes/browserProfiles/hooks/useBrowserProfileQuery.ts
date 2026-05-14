import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { BrowserProfileApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

type UseQueryOptions = Omit<
  Parameters<typeof useQuery<BrowserProfileApiResponse>>[0],
  "queryKey" | "queryFn"
>;

function useBrowserProfileQuery(
  profileId: string | undefined,
  options: UseQueryOptions = {},
) {
  const credentialGetter = useCredentialGetter();

  return useQuery<BrowserProfileApiResponse>({
    queryKey: ["browserProfile", profileId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get<BrowserProfileApiResponse>(`/browser_profiles/${profileId}`)
        .then((response) => response.data);
    },
    enabled: Boolean(profileId),
    staleTime: 5 * 60 * 1000,
    ...options,
  });
}

export { useBrowserProfileQuery };
