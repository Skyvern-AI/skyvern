import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "./useCredentialGetter";
import {
  CreateOnePasswordTokenRequest,
  CreateOnePasswordTokenResponse,
  OnePasswordTokenStatus,
} from "@/api/types";
import { useToast } from "@/components/ui/use-toast";
import { useClearOrganizationAuthToken } from "./useClearOrganizationAuthToken";

export function useOnePasswordToken() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const {
    data: onePasswordStatus,
    isLoading,
    isError,
  } = useQuery<OnePasswordTokenStatus>({
    queryKey: ["onePasswordStatus"],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return await client
        .get("/credentials/onepassword/status")
        .then((response) => response.data as OnePasswordTokenStatus);
    },
  });

  const createOrUpdateTokenMutation = useMutation({
    mutationFn: async (data: CreateOnePasswordTokenRequest) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return await client
        .post("/credentials/onepassword/create", data)
        .then((response) => response.data as CreateOnePasswordTokenResponse);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["onePasswordStatus"] });
      void queryClient.invalidateQueries({ queryKey: ["onepasswordItems"] });
      toast({
        title: "Success",
        description: "1Password service account token updated successfully",
      });
    },
    onError: (error: unknown) => {
      const message =
        (error as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ||
        (error as Error)?.message ||
        "Failed to update 1Password token";
      toast({
        title: "Error",
        description: message,
        variant: "destructive",
      });
    },
  });

  const clearTokenMutation = useClearOrganizationAuthToken({
    providerPath: "onepassword",
    queryKey: "onePasswordStatus",
    invalidateQueryKeys: ["onepasswordItems"],
    setQueryDataOnSuccess: false,
    successDescription: "1Password service account token cleared successfully",
    errorDescription: "Failed to clear 1Password token",
  });

  return {
    onePasswordStatus,
    isLoading,
    isError,
    createOrUpdateToken: createOrUpdateTokenMutation.mutate,
    isUpdating: createOrUpdateTokenMutation.isPending,
    clearToken: clearTokenMutation.mutate,
    isClearing: clearTokenMutation.isPending,
  };
}
