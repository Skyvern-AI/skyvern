import { getClient } from "@/api/AxiosClient";
import { Status, WorkflowRunApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { useGlobalWorkflowsQuery } from "./useGlobalWorkflowsQuery";

type QueryReturnType = Array<WorkflowRunApiResponse>;
type UseQueryOptions = Omit<
  Parameters<typeof useQuery<QueryReturnType>>[0],
  "queryKey" | "queryFn" | "enabled"
>;

type Props = {
  workflowPermanentId?: string;
  statusFilters?: Array<Status>;
  page: number;
  search?: string;
} & UseQueryOptions;

function useWorkflowRunsQuery({
  workflowPermanentId,
  statusFilters,
  page,
  search,
  ...queryOptions
}: Props) {
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const credentialGetter = useCredentialGetter();

  return useQuery<Array<WorkflowRunApiResponse>>({
    queryKey: [
      "workflowRuns",
      { statusFilters },
      workflowPermanentId,
      page,
      search,
    ],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      const isGlobalWorkflow = globalWorkflows?.some(
        (workflow) => workflow.workflow_permanent_id === workflowPermanentId,
      );
      params.append("page", String(page));
      if (isGlobalWorkflow) {
        params.append("template", "true");
      }
      if (statusFilters) {
        statusFilters.forEach((status) => {
          params.append("status", status);
        });
      }
      if (search) {
        params.append("search_key", search);
      }

      return client
        .get(`/workflows/${workflowPermanentId}/runs`, {
          params,
        })
        .then((response) => response.data);
    },
    enabled: !!workflowPermanentId && !!globalWorkflows,
    ...queryOptions,
  });
}

export { useWorkflowRunsQuery };
