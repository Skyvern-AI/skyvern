import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import type {
  RunHealSummary,
  RunsHealSummaryBatchResponse,
} from "../types/healTypes";

// Backend rejects more than this many ids per request; chunk so a long run
// list never drops markers.
const BATCH_RUN_HEAL_MAX_IDS = 100;

// One batch fetch of per-run heal summaries for the visible runs (avoids the
// N+1 of the single-run heal-episodes call). Ids are sorted so the query key
// is order-independent and identical sets share a cache entry. Run ids are
// globally unique, so the key needs no org scope (mirrors the run-tags batch).
function useRunsHealSummaryBatchQuery(workflowRunIds: Array<string>) {
  const credentialGetter = useCredentialGetter();
  const sortedIds = [...workflowRunIds].sort();

  return useQuery<Record<string, RunHealSummary>>({
    queryKey: ["run-heal-summary", "batch", sortedIds],
    enabled: sortedIds.length > 0,
    queryFn: async ({ signal }) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const chunks: Array<Array<string>> = [];
      for (let i = 0; i < sortedIds.length; i += BATCH_RUN_HEAL_MAX_IDS) {
        chunks.push(sortedIds.slice(i, i + BATCH_RUN_HEAL_MAX_IDS));
      }
      const responses = await Promise.all(
        chunks.map((ids) =>
          client
            .post<RunsHealSummaryBatchResponse>(
              "/runs/heal_summary/batch",
              { workflow_run_ids: ids },
              { signal },
            )
            .then((response) => response.data.summaries),
        ),
      );
      // Chunks are disjoint slices, so run-id keys never collide on merge.
      const merged: Record<string, RunHealSummary> = {};
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

export { useRunsHealSummaryBatchQuery };
