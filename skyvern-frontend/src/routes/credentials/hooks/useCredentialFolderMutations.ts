import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import type {
  CredentialFolder,
  CredentialFolderCreate,
  CredentialFolderUpdate,
  UpdateCredentialFolderRequest,
} from "../types/credentialFolderTypes";

function useCreateCredentialFolderMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (data: CredentialFolderCreate) => {
      const client = await getClient(credentialGetter);
      return client
        .post<CredentialFolder>("/credential_folders", data)
        .then((response) => response.data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["credential-folders"] });
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

function useUpdateCredentialFolderMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({
      folderId,
      data,
    }: {
      folderId: string;
      data: CredentialFolderUpdate;
    }) => {
      const client = await getClient(credentialGetter);
      return client
        .put<CredentialFolder>(`/credential_folders/${folderId}`, data)
        .then((response) => response.data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["credential-folders"] });
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

function useDeleteCredentialFolderMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({ folderId }: { folderId: string }) => {
      const client = await getClient(credentialGetter);
      return client.delete(`/credential_folders/${folderId}`);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["credential-folders"] });
      queryClient.invalidateQueries({ queryKey: ["credentials"] });
      toast({
        variant: "success",
        title: "Folder deleted",
        description: "The folder has been deleted.",
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

function useUpdateCredentialFolderAssignmentMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({
      credentialId,
      data,
    }: {
      credentialId: string;
      data: UpdateCredentialFolderRequest;
    }) => {
      const client = await getClient(credentialGetter);
      return client.put(`/credentials/${credentialId}/folder`, data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["credentials"] });
      queryClient.invalidateQueries({ queryKey: ["credential-folders"] });
    },
    onError: (error: Error) => {
      toast({
        variant: "destructive",
        title: "Failed to move credential",
        description: error.message,
      });
    },
  });
}

export {
  useCreateCredentialFolderMutation,
  useUpdateCredentialFolderMutation,
  useDeleteCredentialFolderMutation,
  useUpdateCredentialFolderAssignmentMutation,
};
