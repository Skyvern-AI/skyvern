import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { tagErrorMessage } from "@/routes/workflows/hooks/useWorkflowTagMutations";
import type {
  RunTagsResponse,
  TagApplyRequest,
} from "@/routes/workflows/types/tagTypes";
import {
  type QueryClient,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";

const RUN_TAG_QUERY_KEYS = [
  ["run-tags"],
  ["run-tag-suggestions"],
  ["tag-keys"],
  ["tag-values"],
  ["runs"],
  ["workflowRuns"],
  ["tasks"],
] as const;

function invalidateRunTagQueries(queryClient: QueryClient) {
  for (const queryKey of RUN_TAG_QUERY_KEYS) {
    queryClient.invalidateQueries({ queryKey });
  }
}

function useApplyRunTagsMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({
      workflowRunId,
      data,
    }: {
      workflowRunId: string;
      data: TagApplyRequest;
    }) => {
      const client = await getClient(credentialGetter);
      return client
        .post<RunTagsResponse>(`/runs/${workflowRunId}/tags`, data)
        .then((response) => response.data);
    },
    onSuccess: () => {
      invalidateRunTagQueries(queryClient);
    },
    onError: (error: unknown) => {
      toast({
        variant: "destructive",
        title: "Failed to update run tags",
        description: tagErrorMessage(error),
      });
    },
  });
}

export { invalidateRunTagQueries, useApplyRunTagsMutation };
