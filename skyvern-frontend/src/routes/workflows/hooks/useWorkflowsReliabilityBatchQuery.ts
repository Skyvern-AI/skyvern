import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";
import { useQuery } from "@tanstack/react-query";
import type {
  WorkflowReliability,
  WorkflowsReliabilityBatchResponse,
} from "../types/reliabilityTypes";

// Backend rejects more than this many ids per request; chunk so an oversized
// page never drops badges.
const BATCH_RELIABILITY_MAX_WPIDS = 100;

// One batch fetch of reliability for all visible workflows (avoids per-row
// N+1). Ids are sorted so the query key is order-independent and identical
// sets share a cache entry.
function useWorkflowsReliabilityBatchQuery(
  workflowPermanentIds: Array<string>,
) {
  const credentialGetter = useCredentialGetter();
  const activeOrgId = useActiveOrgId();
  const activeOrgQueryKeyScope = getActiveOrgQueryKeyScope(activeOrgId);
  const sortedIds = [...workflowPermanentIds].sort();

  return useQuery<Record<string, WorkflowReliability>>({
    queryKey: getOrgScopedQueryKey(
      ["workflow-reliability", "batch", sortedIds],
      activeOrgQueryKeyScope,
    ),
    enabled: sortedIds.length > 0,
    queryFn: async ({ signal }) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const chunks: Array<Array<string>> = [];
      for (let i = 0; i < sortedIds.length; i += BATCH_RELIABILITY_MAX_WPIDS) {
        chunks.push(sortedIds.slice(i, i + BATCH_RELIABILITY_MAX_WPIDS));
      }
      const responses = await Promise.all(
        chunks.map((ids) =>
          client
            .post<WorkflowsReliabilityBatchResponse>(
              "/workflows/reliability/batch",
              { workflow_permanent_ids: ids },
              { signal },
            )
            .then((response) => response.data.reliabilities),
        ),
      );
      // Chunks are disjoint slices, so wpid keys never collide on merge.
      const merged: Record<string, WorkflowReliability> = {};
      for (const response of responses) {
        // A response-shape skew (SKY-10683) shipped once already; skip a
        // malformed chunk payload rather than let it inject garbage keys.
        if (response === null || typeof response !== "object") {
          continue;
        }
        Object.assign(merged, response);
      }
      return merged;
    },
  });
}

export { useWorkflowsReliabilityBatchQuery };
