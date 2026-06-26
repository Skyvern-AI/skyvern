import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import {
  normalizeWorkflowTags,
  type Tag,
  type WorkflowTagsBatchResponse,
} from "../types/tagTypes";

// Backend rejects more than this many IDs per request (_BATCH_TAGS_MAX_WPIDS); the
// list page size is unbounded, so chunk rather than slice to avoid dropping tags.
const BATCH_TAGS_MAX_WPIDS = 200;

// One batch fetch for all visible workflows (avoids per-row N+1). Ids are sorted so
// the query key is order-independent and identical sets share a cache entry.
function useWorkflowTagsBatchQuery(
  workflowPermanentIds: Array<string>,
  { enabled = true }: { enabled?: boolean } = {},
) {
  const credentialGetter = useCredentialGetter();
  const sortedIds = [...workflowPermanentIds].sort();

  return useQuery({
    queryKey: ["workflow-tags", "batch", sortedIds],
    enabled: enabled && sortedIds.length > 0,
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
      // Normalize per workflow: the SKY-10683 backend/frontend split shipped a
      // response-shape skew once already; never let one reach render again.
      const merged: Record<string, Array<Tag>> = {};
      for (const response of responses) {
        if (response === null || typeof response !== "object") {
          continue;
        }
        for (const [wpid, tags] of Object.entries(response)) {
          merged[wpid] = normalizeWorkflowTags(tags);
        }
      }
      return merged;
    },
  });
}

export { useWorkflowTagsBatchQuery };
