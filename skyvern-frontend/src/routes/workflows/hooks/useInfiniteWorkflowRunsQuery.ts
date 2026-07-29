import { useInfiniteQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { WorkflowRunApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";

import { useGlobalWorkflowsQuery } from "./useGlobalWorkflowsQuery";

// Infinite (scroll-paged) history of a workflow's runs, for the Past Runs
// selector. Mirrors useFolderWorkflowsQuery / useInfiniteCopilotChatsQuery.
function useInfiniteWorkflowRunsQuery({
  workflowPermanentId,
  pageSize = 20,
  enabled,
  refetchInterval,
}: {
  workflowPermanentId?: string;
  pageSize?: number;
  // ANDed with the internal gating (workflow id + globalWorkflows loaded).
  enabled?: boolean;
  refetchInterval?: number | false;
}) {
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const credentialGetter = useCredentialGetter();
  const activeOrgId = useActiveOrgId();
  const activeOrgQueryKeyScope = getActiveOrgQueryKeyScope(activeOrgId);

  return useInfiniteQuery({
    queryKey: getOrgScopedQueryKey(
      ["workflowRuns", "infinite", workflowPermanentId, pageSize],
      activeOrgQueryKeyScope,
    ),
    queryFn: async ({ pageParam = 1, signal }) => {
      const client = await getClient(credentialGetter);
      const isGlobalWorkflow = globalWorkflows?.some(
        (workflow) => workflow.workflow_permanent_id === workflowPermanentId,
      );
      const params = new URLSearchParams();
      params.append("page", String(pageParam));
      params.append("page_size", String(pageSize));
      if (isGlobalWorkflow) {
        params.append("template", "true");
      }
      return client
        .get<
          Array<WorkflowRunApiResponse>
        >(`/workflows/${workflowPermanentId}/runs`, { params, signal })
        .then((response) => response.data);
    },
    // A full page implies a possible next one; an exact-multiple total costs
    // one empty fetch.
    getNextPageParam: (lastPage, allPages) =>
      lastPage.length === pageSize ? allPages.length + 1 : undefined,
    initialPageParam: 1,
    enabled: !!workflowPermanentId && !!globalWorkflows && (enabled ?? true),
    // ponytail: a poll refetches every loaded page; runs history is short and
    // this only fires while the popover is open, so accept it rather than
    // capping pages.
    refetchInterval,
  });
}

export { useInfiniteWorkflowRunsQuery };
