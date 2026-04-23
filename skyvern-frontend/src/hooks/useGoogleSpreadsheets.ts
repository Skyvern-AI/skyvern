import { useInfiniteQuery } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import type { PagedGoogleSpreadsheets } from "@/api/types";

type Options = {
  credentialId: string;
  query: string;
  enabled: boolean;
};

const PAGE_SIZE = 25;

export function useGoogleSpreadsheets({
  credentialId,
  query,
  enabled,
}: Options) {
  const credentialGetter = useCredentialGetter();

  return useInfiniteQuery<PagedGoogleSpreadsheets>({
    queryKey: ["googleSheets", "spreadsheets", credentialId, query],
    queryFn: async ({ pageParam }) => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("credential_id", credentialId);
      params.append("page_size", String(PAGE_SIZE));
      if (query) params.append("q", query);
      if (pageParam) params.append("page_token", String(pageParam));
      const response = await client.get("/google/sheets/spreadsheets", {
        params,
      });
      return response.data as PagedGoogleSpreadsheets;
    },
    getNextPageParam: (last) => last.next_page_token ?? undefined,
    initialPageParam: "",
    enabled: enabled && Boolean(credentialId),
  });
}
