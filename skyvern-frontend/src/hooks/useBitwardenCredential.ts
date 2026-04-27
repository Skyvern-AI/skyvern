import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "./useCredentialGetter";
import {
  BitwardenCredentialResponse,
  BitwardenOrganizationAuthToken,
  CreateBitwardenCredentialRequest,
} from "@/api/types";
import { useToast } from "@/components/ui/use-toast";

export function useBitwardenCredential() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const { data: bitwardenOrganizationAuthToken, isLoading } =
    useQuery<BitwardenOrganizationAuthToken>({
      queryKey: ["bitwardenOrganizationAuthToken"],
      queryFn: async () => {
        const client = await getClient(credentialGetter, "sans-api-v1");
        return await client
          .get("/credentials/bitwarden/get")
          .then((response) => response.data.token)
          .catch((error: unknown) => {
            const status = (error as { response?: { status?: number } })
              ?.response?.status;
            if (status === 404) return null;
            throw error;
          });
      },
    });

  const createOrUpdateTokenMutation = useMutation({
    mutationFn: async (data: CreateBitwardenCredentialRequest) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return await client
        .post("/credentials/bitwarden/create", data)
        .then((response) => response.data as BitwardenCredentialResponse);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["bitwardenOrganizationAuthToken"],
      });
      toast({
        title: "Success",
        description: "Bitwarden credential updated successfully",
      });
    },
    onError: (error: unknown) => {
      const message =
        (error as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ||
        (error as Error)?.message ||
        "Failed to update Bitwarden credential";
      toast({
        title: "Error",
        description: message,
        variant: "destructive",
      });
    },
  });

  return {
    bitwardenOrganizationAuthToken,
    isLoading,
    createOrUpdateToken: createOrUpdateTokenMutation.mutate,
    isUpdating: createOrUpdateTokenMutation.isPending,
  };
}
