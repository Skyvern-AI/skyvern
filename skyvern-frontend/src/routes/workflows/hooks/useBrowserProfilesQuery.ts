import { getClient } from "@/api/AxiosClient";
import { BrowserProfileApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";

type QueryReturnType = Array<BrowserProfileApiResponse>;
type UseQueryOptions = Omit<
  Parameters<typeof useQuery<QueryReturnType>>[0],
  "queryKey" | "queryFn"
>;

type Props = UseQueryOptions & {
  includeDeleted?: boolean;
  page?: number;
  page_size?: number;
};

function useBrowserProfilesQuery(props: Props = {}) {
  const { includeDeleted = false, page, page_size, ...queryOptions } = props;
  const credentialGetter = useCredentialGetter();

  return useQuery<QueryReturnType>({
    queryKey: ["browserProfiles", includeDeleted, page, page_size],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const params = new URLSearchParams();
      if (includeDeleted) {
        params.set("include_deleted", "true");
      }
      if (page !== undefined) {
        params.set("page", String(page));
      }
      if (page_size !== undefined) {
        params.set("page_size", String(page_size));
      }
      return client
        .get("/browser_profiles", { params })
        .then((res) => res.data);
    },
    staleTime: 5 * 60 * 1000,
    ...queryOptions,
  });
}

export { useBrowserProfilesQuery };
