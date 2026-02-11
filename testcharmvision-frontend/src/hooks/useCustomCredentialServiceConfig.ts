import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "./useCredentialGetter";
import {
  CustomCredentialServiceConfigResponse,
  CustomCredentialServiceOrganizationAuthToken,
  CreateCustomCredentialServiceConfigRequest,
  CustomCredentialServiceConfig,
} from "@/api/types";
import { useToast } from "@/components/ui/use-toast";

export function useCustomCredentialServiceConfig() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const { data: customCredentialServiceAuthToken, isLoading } =
    useQuery<CustomCredentialServiceOrganizationAuthToken>({
      queryKey: ["customCredentialServiceAuthToken"],
      queryFn: async () => {
        const client = await getClient(credentialGetter, "sans-api-v1");
        return await client
          .get("/credentials/custom_credential/get")
          .then((response) => response.data.token)
          .catch((error) => {
            // 404 likely means not configured yet - return null silently
            if (error?.response?.status === 404) {
              return null;
            }
            // Log other errors for debugging but still return null
            console.warn(
              "Failed to fetch custom credential service config:",
              error,
            );
            return null;
          });
      },
    });

  // Parse the configuration from the stored token
  const parsedConfig: CustomCredentialServiceConfig | null = useMemo(() => {
    if (!customCredentialServiceAuthToken?.token) return null;

    try {
      return JSON.parse(customCredentialServiceAuthToken.token);
    } catch {
      return null;
    }
  }, [customCredentialServiceAuthToken?.token]);

  const createOrUpdateConfigMutation = useMutation({
    mutationFn: async (data: CreateCustomCredentialServiceConfigRequest) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return await client
        .post("/credentials/custom_credential/create", data)
        .then(
          (response) => response.data as CustomCredentialServiceConfigResponse,
        );
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["customCredentialServiceAuthToken"],
      });
      toast({
        title: "Success",
        description:
          "Custom credential service configuration updated successfully",
      });
    },
    onError: (error: unknown) => {
      const message =
        (error as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ||
        (error as Error)?.message ||
        "Failed to update custom credential service configuration";
      toast({
        title: "Error",
        description: message,
        variant: "destructive",
      });
    },
  });

  return {
    customCredentialServiceAuthToken,
    parsedConfig,
    isLoading,
    createOrUpdateConfig: createOrUpdateConfigMutation.mutate,
    isUpdating: createOrUpdateConfigMutation.isPending,
  };
}
