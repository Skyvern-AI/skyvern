import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { Status, TaskRunListItem, TaskRunType } from "@/api/types";
import {
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";

type QueryReturnType = Array<TaskRunListItem>;
type UseQueryOptions = Omit<
  Parameters<typeof useQuery<QueryReturnType>>[0],
  "queryKey" | "queryFn"
>;

type Props = {
  page?: number;
  pageSize?: number;
  statusFilters?: Array<Status>;
  runTypeFilters?: Array<TaskRunType>;
  search?: string;
} & UseQueryOptions;

function useRunsQuery({
  page = 1,
  pageSize = 10,
  statusFilters,
  runTypeFilters,
  search,
  ...queryOptions
}: Props) {
  const credentialGetter = useCredentialGetter();
  const activeOrgId = useActiveOrgId();
  const activeOrgQueryKeyScope = getActiveOrgQueryKeyScope(activeOrgId);
  return useQuery<Array<TaskRunListItem>>({
    queryKey: getOrgScopedQueryKey(
      ["runs", { statusFilters, runTypeFilters }, page, pageSize, search],
      activeOrgQueryKeyScope,
    ),
    queryFn: async ({ signal }) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const params = new URLSearchParams();
      params.append("page", String(page));
      params.append("page_size", String(pageSize));
      if (statusFilters) {
        statusFilters.forEach((status) => {
          params.append("status", status);
        });
      }
      if (runTypeFilters) {
        runTypeFilters.forEach((runType) => {
          params.append("run_type", runType);
        });
      }
      if (search) {
        params.append("search_key", search);
      }
      return client.get("/runs", { params, signal }).then((res) => res.data);
    },
    ...queryOptions,
  });
}

export { useRunsQuery };
