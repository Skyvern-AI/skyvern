import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { type BrowserSession } from "@/routes/workflows/types/browserSessionTypes";

const useBrowserSessionsQuery = (page: number, itemsPerPage: number) => {
  const credentialGetter = useCredentialGetter();

  return useQuery<BrowserSession[]>({
    queryKey: ["browser_sessions", page, itemsPerPage],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const params = new URLSearchParams();
      params.append("page", String(page));
      params.append("page_size", String(itemsPerPage));
      return client
        .get(`/browser_sessions/history`, {
          params,
        })
        .then((response) => response.data);
    },
  });
};

export { useBrowserSessionsQuery };
