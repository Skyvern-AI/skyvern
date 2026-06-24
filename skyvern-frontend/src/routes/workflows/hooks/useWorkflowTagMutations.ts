import { useMutation, useQueryClient } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import type { TagApplyRequest, TagKey, TagsResponse } from "../types/tagTypes";

// Tag writes put validation/conflict reasons in the FastAPI `detail` (422 cap/regex,
// 409 concurrent write), so prefer it over the generic axios message.
function tagErrorMessage(error: unknown): string {
  if (isAxiosError(error)) {
    const detail = error.response?.data?.detail;
    if (typeof detail === "string") {
      return detail;
    }
  }
  return error instanceof Error ? error.message : "Unknown error";
}

function useApplyWorkflowTagsMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({
      workflowPermanentId,
      data,
    }: {
      workflowPermanentId: string;
      data: TagApplyRequest;
    }) => {
      const client = await getClient(credentialGetter);
      return client
        .post<TagsResponse>(`/workflows/${workflowPermanentId}/tags`, data)
        .then((response) => response.data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workflow-tags"] });
      // A first-time key registers a new tag-key row, so refresh the registry.
      queryClient.invalidateQueries({ queryKey: ["tag-keys"] });
      // The apply request can set/change a grouped tag's color, so refresh the
      // color registry or new colors won't render until a hard refetch.
      queryClient.invalidateQueries({ queryKey: ["tag-values"] });
      // The workflows list is filterable by tags, so a changed tag can change
      // which rows match the active ?tags= filter.
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (error: unknown) => {
      toast({
        variant: "destructive",
        title: "Failed to update tags",
        description: tagErrorMessage(error),
      });
    },
  });
}

// Per-tag removal goes through the apply mutation's `tags_to_delete` (grouped by
// {key}, standalone by {value}) so one path handles both.

// Exported for the not-yet-wired tag-key description editor so that surface can
// consume it without re-deriving the hook.
function useUpdateTagKeyMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({
      key,
      description,
    }: {
      key: string;
      description: string | null;
    }) => {
      const client = await getClient(credentialGetter);
      return client
        .patch<TagKey>(`/tag-keys/${encodeURIComponent(key)}`, { description })
        .then((response) => response.data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tag-keys"] });
    },
    onError: (error: unknown) => {
      toast({
        variant: "destructive",
        title: "Failed to update tag key",
        description: tagErrorMessage(error),
      });
    },
  });
}

function useDeleteTagKeyMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (key: string) => {
      const client = await getClient(credentialGetter);
      return client
        .delete(`/tag-keys/${encodeURIComponent(key)}`)
        .then((response) => response.data);
    },
    onSuccess: () => {
      // Cascade delete drops the key and removes the tag from every workflow; refresh
      // the registry, batch tags, and the (tag-filterable) workflows list.
      queryClient.invalidateQueries({ queryKey: ["tag-keys"] });
      queryClient.invalidateQueries({ queryKey: ["workflow-tags"] });
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
    },
    onError: (error: unknown) => {
      toast({
        variant: "destructive",
        title: "Failed to delete tag",
        description: tagErrorMessage(error),
      });
    },
  });
}

export {
  useApplyWorkflowTagsMutation,
  useUpdateTagKeyMutation,
  useDeleteTagKeyMutation,
};
