import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useWorkflowTaggingEnabled } from "@/hooks/useWorkflowTaggingEnabled";
import { useQuery } from "@tanstack/react-query";
import {
  normalizeWorkflowTags,
  type RunTagsResponse,
} from "@/routes/workflows/types/tagTypes";

function useRunTagsQuery(
  workflowRunId: string | null | undefined,
  { enabled = true }: { enabled?: boolean } = {},
) {
  const credentialGetter = useCredentialGetter();
  const taggingEnabled = useWorkflowTaggingEnabled();

  return useQuery({
    queryKey: ["run-tags", workflowRunId],
    enabled: enabled && taggingEnabled && !!workflowRunId,
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get<RunTagsResponse>(`/runs/${workflowRunId}/tags`)
        .then((response) =>
          normalizeWorkflowTags(response.data.tags).map((tag) => ({
            key: tag.key,
            value: tag.value,
          })),
        );
    },
  });
}

export { useRunTagsQuery };
