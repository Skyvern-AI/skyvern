import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { BrowserProfile } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

type UseBrowserProfilesQueryOptions = {
  includeDeleted?: boolean;
};

function useBrowserProfilesQuery({
  includeDeleted = false,
}: UseBrowserProfilesQueryOptions = {}) {
  const credentialGetter = useCredentialGetter();

  return useQuery<BrowserProfile[]>({
    queryKey: ["browserProfiles", includeDeleted],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const params = new URLSearchParams();
      if (includeDeleted) {
        params.set("include_deleted", "true");
      }

      return client
        .get("/browser_profiles", { params })
        .then((response) => response.data);
    },
  });
}

export { useBrowserProfilesQuery };
