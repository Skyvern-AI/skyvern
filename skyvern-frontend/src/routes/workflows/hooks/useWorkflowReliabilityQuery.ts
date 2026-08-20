import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";
import { useQuery } from "@tanstack/react-query";
import type { WorkflowReliability } from "../types/reliabilityTypes";

type UseWorkflowReliabilityQueryOptions = {
  workflowPermanentId?: string;
  enabled?: boolean;
};

function useWorkflowReliabilityQuery({
  workflowPermanentId,
  enabled = true,
}: UseWorkflowReliabilityQueryOptions) {
  const credentialGetter = useCredentialGetter();
  const activeOrgId = useActiveOrgId();
  const activeOrgQueryKeyScope = getActiveOrgQueryKeyScope(activeOrgId);

  return useQuery<WorkflowReliability>({
    queryKey: getOrgScopedQueryKey(
      ["workflow-reliability", workflowPermanentId],
      activeOrgQueryKeyScope,
    ),
    queryFn: async ({ signal }) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get<WorkflowReliability>(
          `/workflows/${workflowPermanentId}/reliability`,
          {
            signal,
          },
        )
        .then((response) => response.data);
    },
    enabled: Boolean(workflowPermanentId) && enabled,
  });
}

export { useWorkflowReliabilityQuery };
