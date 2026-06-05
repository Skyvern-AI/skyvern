import { useMutation, useQueryClient } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import type { TagApplyRequest, TagKey, TagsResponse } from "../types/tagTypes";

// Tag writes surface validation/conflict reasons in the FastAPI `detail`
// string (422 cap/regex, 409 concurrent write), so prefer it over the generic
// axios message.
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

function useDeleteWorkflowTagMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({
      workflowPermanentId,
      key,
    }: {
      workflowPermanentId: string;
      key: string;
    }) => {
      const client = await getClient(credentialGetter);
      return client
        .delete<TagsResponse>(
          `/workflows/${workflowPermanentId}/tags/${encodeURIComponent(key)}`,
        )
        .then((response) => response.data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workflow-tags"] });
      // Removing a tag decrements the key's workflow_count (and a last-remove
      // drops it to 0), so refresh the registry that feeds the filter dropdown.
      queryClient.invalidateQueries({ queryKey: ["tag-keys"] });
      // A removed tag can drop the row out of the active ?tags= filter.
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

// Part of the tag-key mutation surface (SKY-10660). Not yet wired to a UI —
// the tag-key description editor is a queued follow-up (SKY-10661 optional) —
// but exported so that surface can consume it without re-deriving the hook.
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
      // Cascade delete drops the key from the registry AND removes the tag from
      // every workflow, so refresh the registry, batch tags, and the (tag-
      // filterable) workflows list.
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
  useDeleteWorkflowTagMutation,
  useUpdateTagKeyMutation,
  useDeleteTagKeyMutation,
};
