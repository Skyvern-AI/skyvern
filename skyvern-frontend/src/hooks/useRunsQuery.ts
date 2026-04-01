import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { Status, Task, TriggerType, WorkflowRunApiResponse } from "@/api/types";

type QueryReturnType = Array<Task | WorkflowRunApiResponse>;
type UseQueryOptions = Omit<
  Parameters<typeof useQuery<QueryReturnType>>[0],
  "queryKey" | "queryFn"
>;

type Props = {
  page?: number;
  pageSize?: number;
  statusFilters?: Array<Status>;
  triggerTypeFilters?: Array<TriggerType>;
  search?: string;
} & UseQueryOptions;

function useRunsQuery({
  page = 1,
  pageSize = 10,
  statusFilters,
  triggerTypeFilters,
  search,
  ...queryOptions
}: Props) {
  const credentialGetter = useCredentialGetter();
  return useQuery<Array<Task | WorkflowRunApiResponse>>({
    ...queryOptions,
    queryKey: [
      "runs",
      { statusFilters, triggerTypeFilters },
      page,
      pageSize,
      search,
    ],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("page", String(page));
      params.append("page_size", String(pageSize));
      if (statusFilters) {
        statusFilters.forEach((status) => {
          params.append("status", status);
        });
      }
      if (triggerTypeFilters) {
        triggerTypeFilters.forEach((triggerType) => {
          params.append("trigger_type", triggerType);
        });
      }
      if (search) {
        params.append("search_key", search);
      }
      return client.get("/runs", { params }).then((res) => res.data);
    },
  });
}

export { useRunsQuery };
