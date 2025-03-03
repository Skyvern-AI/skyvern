import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { Status, Task, WorkflowRunApiResponse } from "@/api/types";

type QueryReturnType = Array<Task | WorkflowRunApiResponse>;
type UseQueryOptions = Omit<
  Parameters<typeof useQuery<QueryReturnType>>[0],
  "queryKey" | "queryFn"
>;

type Props = {
  page?: number;
  statusFilters?: Array<Status>;
} & UseQueryOptions;

function useRunsQuery({ page = 1, statusFilters }: Props) {
  const credentialGetter = useCredentialGetter();
  return useQuery<Array<Task | WorkflowRunApiResponse>>({
    queryKey: ["runs", { statusFilters }, page],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("page", String(page));
      if (statusFilters) {
        statusFilters.forEach((status) => {
          params.append("status", status);
        });
      }
      return client.get("/runs", { params }).then((res) => res.data);
    },
  });
}

export { useRunsQuery };
