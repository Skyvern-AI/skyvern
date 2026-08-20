import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import {
  normalizeWorkflowTags,
  type RunTagsBatchResponse,
  type Tag,
} from "@/routes/workflows/types/tagTypes";

const BATCH_RUN_TAGS_MAX_IDS = 200;

function useRunTagsBatchQuery(
  workflowRunIds: Array<string>,
  { enabled = true }: { enabled?: boolean } = {},
) {
  const credentialGetter = useCredentialGetter();
  const sortedIds = [...workflowRunIds].sort();

  return useQuery({
    queryKey: ["run-tags", "batch", sortedIds],
    enabled: enabled && sortedIds.length > 0,
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const chunks: Array<Array<string>> = [];
      for (let i = 0; i < sortedIds.length; i += BATCH_RUN_TAGS_MAX_IDS) {
        chunks.push(sortedIds.slice(i, i + BATCH_RUN_TAGS_MAX_IDS));
      }

      const responses = await Promise.all(
        chunks.map((ids) => {
          const params = new URLSearchParams();
          params.append("workflow_run_ids", ids.join(","));
          return client
            .get<RunTagsBatchResponse>("/run-tags", { params })
            .then((response) => response.data.run_tags);
        }),
      );

      const merged: Record<string, Array<Tag>> = {};
      for (const response of responses) {
        if (response === null || typeof response !== "object") {
          continue;
        }
        for (const [workflowRunId, tags] of Object.entries(response)) {
          merged[workflowRunId] = normalizeWorkflowTags(tags);
        }
      }
      return merged;
    },
  });
}

export { useRunTagsBatchQuery };
