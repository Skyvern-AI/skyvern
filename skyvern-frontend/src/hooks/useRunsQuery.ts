import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { Status, TaskRunListItem } from "@/api/types";

type QueryReturnType = Array<TaskRunListItem>;
type UseQueryOptions = Omit<
  Parameters<typeof useQuery<QueryReturnType>>[0],
  "queryKey" | "queryFn"
>;

type Props = {
  page?: number;
  pageSize?: number;
  statusFilters?: Array<Status>;
  search?: string;
} & UseQueryOptions;

function useRunsQuery({
  page = 1,
  pageSize = 10,
  statusFilters,
  search,
  ...queryOptions
}: Props) {
  const credentialGetter = useCredentialGetter();
  return useQuery<Array<TaskRunListItem>>({
    queryKey: ["runs", { statusFilters }, page, pageSize, search],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const params = new URLSearchParams();
      params.append("page", String(page));
      params.append("page_size", String(pageSize));
      if (statusFilters) {
        statusFilters.forEach((status) => {
          params.append("status", status);
        });
      }
      if (search) {
        params.append("search_key", search);
      }
      return client.get("/runs", { params }).then((res) => res.data);
    },
    ...queryOptions,
  });
}

export { useRunsQuery };
