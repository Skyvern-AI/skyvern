import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import type { WorkflowTagsBatchResponse } from "../types/tagTypes";

// The backend batch endpoint rejects more than this many IDs per request
// (_BATCH_TAGS_MAX_WPIDS). The workflows list page size is unbounded via the
// URL, so chunk rather than slice to avoid silently dropping tags for rows
// beyond the limit. The common case (page size <= 50) is a single request.
const BATCH_TAGS_MAX_WPIDS = 200;

// One batch fetch for all visible workflows, avoiding an N+1 of per-row tag
// fetches on the workflows-list page. Ids are sorted so the query key is
// order-independent and two renders with the same set share a cache entry.
function useWorkflowTagsBatchQuery(workflowPermanentIds: Array<string>) {
  const credentialGetter = useCredentialGetter();
  const sortedIds = [...workflowPermanentIds].sort();

  return useQuery({
    queryKey: ["workflow-tags", "batch", sortedIds],
    enabled: sortedIds.length > 0,
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const chunks: Array<Array<string>> = [];
      for (let i = 0; i < sortedIds.length; i += BATCH_TAGS_MAX_WPIDS) {
        chunks.push(sortedIds.slice(i, i + BATCH_TAGS_MAX_WPIDS));
      }
      const responses = await Promise.all(
        chunks.map((ids) => {
          const params = new URLSearchParams();
          params.append("workflow_permanent_ids", ids.join(","));
          return client
            .get<WorkflowTagsBatchResponse>("/workflow-tags", { params })
            .then((response) => response.data.workflow_tags);
        }),
      );
      // Chunks are disjoint slices, so wpid keys never collide on merge.
      const merged: Record<string, Record<string, string>> = {};
      for (const response of responses) {
        Object.assign(merged, response);
      }
      return merged;
    },
  });
}

export { useWorkflowTagsBatchQuery };
