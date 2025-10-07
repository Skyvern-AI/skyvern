import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "./useCredentialGetter";
import {
  AzureClientSecretCredentialResponse,
  AzureOrganizationAuthToken,
  CreateAzureClientSecretCredentialRequest,
} from "@/api/types";
import { useToast } from "@/components/ui/use-toast";

export function useAzureClientCredentialToken() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const { data: azureOrganizationAuthToken, isLoading } =
    useQuery<AzureOrganizationAuthToken>({
      queryKey: ["azureOrganizationAuthToken"],
      queryFn: async () => {
        const client = await getClient(credentialGetter, "sans-api-v1");
        return await client
          .get("/credentials/azure_credential/get")
          .then((response) => response.data.token)
          .catch(() => null);
      },
    });

  const createOrUpdateTokenMutation = useMutation({
    mutationFn: async (data: CreateAzureClientSecretCredentialRequest) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return await client
        .post("/credentials/azure_credential/create", data)
        .then(
          (response) => response.data as AzureClientSecretCredentialResponse,
        );
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["azureOrganizationAuthToken"],
      });
      toast({
        title: "Success",
        description: "Azure Client Secret Credential updated successfully",
      });
    },
    onError: (error: unknown) => {
      const message =
        (error as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ||
        (error as Error)?.message ||
        "Failed to update Azure Client Secret Credential";
      toast({
        title: "Error",
        description: message,
        variant: "destructive",
      });
    },
  });

  return {
    azureOrganizationAuthToken,
    isLoading,
    createOrUpdateToken: createOrUpdateTokenMutation.mutate,
    isUpdating: createOrUpdateTokenMutation.isPending,
  };
}
