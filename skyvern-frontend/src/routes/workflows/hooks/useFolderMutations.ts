import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import type {
  Folder,
  FolderCreate,
  FolderUpdate,
  UpdateWorkflowFolderRequest,
} from "../types/folderTypes";

function useCreateFolderMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (data: FolderCreate) => {
      const client = await getClient(credentialGetter);
      return client
        .post<Folder>("/folders", data)
        .then((response) => response.data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["folders"] });
      toast({
        title: "Folder created",
        description: "Successfully created folder",
      });
    },
    onError: (error: Error) => {
      toast({
        variant: "destructive",
        title: "Failed to create folder",
        description: error.message,
      });
    },
  });
}

function useUpdateFolderMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({
      folderId,
      data,
    }: {
      folderId: string;
      data: FolderUpdate;
    }) => {
      const client = await getClient(credentialGetter);
      return client
        .put<Folder>(`/folders/${folderId}`, data)
        .then((response) => response.data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["folders"] });
      toast({
        title: "Folder updated",
        description: "Successfully updated folder",
      });
    },
    onError: (error: Error) => {
      toast({
        variant: "destructive",
        title: "Failed to update folder",
        description: error.message,
      });
    },
  });
}

function useDeleteFolderMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (folderId: string) => {
      const client = await getClient(credentialGetter);
      return client.delete(`/folders/${folderId}`);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["folders"] });
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      toast({
        title: "Folder deleted",
        description: "Successfully deleted folder",
      });
    },
    onError: (error: Error) => {
      toast({
        variant: "destructive",
        title: "Failed to delete folder",
        description: error.message,
      });
    },
  });
}

function useUpdateWorkflowFolderMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({
      workflowId,
      data,
    }: {
      workflowId: string;
      data: UpdateWorkflowFolderRequest;
    }) => {
      const client = await getClient(credentialGetter);
      return client.put(`/workflows/${workflowId}/folder`, data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      queryClient.invalidateQueries({ queryKey: ["folders"] });
      toast({
        title: "Workflow folder updated",
        description: "Successfully updated workflow folder",
      });
    },
    onError: (error: Error) => {
      toast({
        variant: "destructive",
        title: "Failed to update workflow folder",
        description: error.message,
      });
    },
  });
}

export {
  useCreateFolderMutation,
  useUpdateFolderMutation,
  useDeleteFolderMutation,
  useUpdateWorkflowFolderMutation,
};

