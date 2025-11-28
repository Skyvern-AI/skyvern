import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
          .catch(() => null);
      },
    });

  // Parse the configuration from the stored token
  const parsedConfig: CustomCredentialServiceConfig | null = (() => {
    if (!customCredentialServiceAuthToken?.token) return null;

    try {
      return JSON.parse(customCredentialServiceAuthToken.token);
    } catch {
      return null;
    }
  })();

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
        description: "Custom credential service configuration updated successfully",
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

  const testConnectionMutation = useMutation({
    mutationFn: async (config: CustomCredentialServiceConfig) => {
      // Test the connection by making a request to the base API URL
      const testUrl = config.api_base_url.replace(/\/$/, '');

      try {
        // Create an AbortController for timeout handling
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 10000); // 10 second timeout

        const response = await fetch(testUrl, {
          method: 'GET',
          headers: {
            'Authorization': `Bearer ${config.api_token}`,
            'Content-Type': 'application/json',
          },
          signal: controller.signal,
        });

        clearTimeout(timeoutId);

        // Consider connection successful if we can reach the API and get any response
        // (even 404, 405, etc. - these indicate the server is reachable)
        if (response.status >= 200 && response.status < 500) {
          if (response.status === 401 || response.status === 403) {
            return {
              success: true,
              message: "Connection successful (authentication may need verification)"
            };
          }
          if (response.status === 404 || response.status === 405) {
            return {
              success: true,
              message: "Connection successful (API endpoint reachable)"
            };
          }
          return { success: true, message: "Connection successful" };
        }

        // Only treat 5xx errors as connection failures
        throw new Error(`Server error: HTTP ${response.status}`);
      } catch (error) {
        if (error instanceof Error && error.name === 'AbortError') {
          throw new Error('Connection timeout after 10 seconds');
        }

        // Network errors, DNS failures, etc.
        if (error instanceof TypeError && error.message.includes('fetch')) {
          throw new Error('Network error: Cannot reach the API server. Check the URL and network connectivity.');
        }

        throw new Error(
          `Connection failed: ${error instanceof Error ? error.message : 'Unknown error'}`
        );
      }
    },
    onSuccess: (data) => {
      toast({
        title: "Connection Test Successful",
        description: data.message,
      });
    },
    onError: (error: unknown) => {
      const message =
        (error as Error)?.message || "Connection test failed";
      toast({
        title: "Connection Test Failed",
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
    testConnection: testConnectionMutation.mutate,
    isTesting: testConnectionMutation.isPending,
  };
}
