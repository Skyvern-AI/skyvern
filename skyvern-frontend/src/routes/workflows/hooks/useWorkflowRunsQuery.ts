import { getClient } from "@/api/AxiosClient";
import { Status, WorkflowRunApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { useGlobalWorkflowsQuery } from "./useGlobalWorkflowsQuery";
import {
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";

type QueryReturnType = Array<WorkflowRunApiResponse>;
type UseQueryOptions = Omit<
  Parameters<typeof useQuery<QueryReturnType>>[0],
  "queryKey" | "queryFn" | "enabled"
>;

type Props = {
  workflowPermanentId?: string;
  statusFilters?: Array<Status>;
  page: number;
  pageSize?: number;
  search?: string;
  // ANDed with the internal gating (workflow id + globalWorkflows loaded).
  enabled?: boolean;
  createdAtStart?: string;
  createdAtEnd?: string;
  tags?: string;
} & UseQueryOptions;

function useWorkflowRunsQuery({
  workflowPermanentId,
  statusFilters,
  page,
  pageSize,
  search,
  enabled,
  createdAtStart,
  createdAtEnd,
  tags,
  ...queryOptions
}: Props) {
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const credentialGetter = useCredentialGetter();
  const activeOrgId = useActiveOrgId();
  const activeOrgQueryKeyScope = getActiveOrgQueryKeyScope(activeOrgId);

  return useQuery<Array<WorkflowRunApiResponse>>({
    queryKey: getOrgScopedQueryKey(
      [
        "workflowRuns",
        { statusFilters, tags },
        workflowPermanentId,
        page,
        pageSize,
        search,
        createdAtStart,
        createdAtEnd,
      ],
      activeOrgQueryKeyScope,
    ),
    queryFn: async ({ signal }) => {
      // The default legacy route supports ?tags= and keeps child workflow
      // runs visible; the /v1 route excludes them, so don't switch on filter.
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      const isGlobalWorkflow = globalWorkflows?.some(
        (workflow) => workflow.workflow_permanent_id === workflowPermanentId,
      );
      params.append("page", String(page));
      if (pageSize) {
        params.append("page_size", String(pageSize));
      }
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
      if (createdAtStart) {
        params.append("created_at_start", createdAtStart);
      }
      if (createdAtEnd) {
        params.append("created_at_end", createdAtEnd);
      }
      if (tags) {
        params.append("tags", tags);
      }

      return client
        .get(`/workflows/${workflowPermanentId}/runs`, {
          params,
          signal,
        })
        .then((response) => response.data);
    },
    enabled: !!workflowPermanentId && !!globalWorkflows && (enabled ?? true),
    ...queryOptions,
  });
}

export { useWorkflowRunsQuery };
