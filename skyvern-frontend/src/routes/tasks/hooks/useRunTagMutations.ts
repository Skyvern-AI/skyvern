import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { tagErrorMessage } from "@/routes/workflows/hooks/useWorkflowTagMutations";
import type {
  RunTagsResponse,
  TagApplyRequest,
} from "@/routes/workflows/types/tagTypes";
import { useMutation, useQueryClient } from "@tanstack/react-query";

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
      queryClient.invalidateQueries({ queryKey: ["run-tags"] });
      queryClient.invalidateQueries({ queryKey: ["tag-keys"] });
      queryClient.invalidateQueries({ queryKey: ["tag-values"] });
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
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

function useDeleteRunTagMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({
      workflowRunId,
      key,
    }: {
      workflowRunId: string;
      key: string;
    }) => {
      const client = await getClient(credentialGetter);
      return client
        .delete(`/runs/${workflowRunId}/tags/${encodeURIComponent(key)}`)
        .then((response) => response.data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["run-tags"] });
      queryClient.invalidateQueries({ queryKey: ["tag-keys"] });
      queryClient.invalidateQueries({ queryKey: ["tag-values"] });
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
    onError: (error: unknown) => {
      toast({
        variant: "destructive",
        title: "Failed to delete run tag",
        description: tagErrorMessage(error),
      });
    },
  });
}

export { useApplyRunTagsMutation, useDeleteRunTagMutation };
