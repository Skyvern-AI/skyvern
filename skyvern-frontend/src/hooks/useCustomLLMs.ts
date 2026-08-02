import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import {
  ClearOrganizationAuthTokenResponse,
  CustomLLM,
  CustomLLMCreateRequest,
  CustomLLMListResponse,
  CustomLLMResponse,
  CustomLLMUpdateRequest,
} from "@/api/types";
import { useToast } from "@/components/ui/use-toast";
import { llmDiagnosticsQueryKey } from "@/hooks/useLLMDiagnostics";
import { useCredentialGetter } from "./useCredentialGetter";

const customLLMsQueryKey = ["customLLMs"] as const;
const modelsQueryKey = ["models"] as const;

function getErrorMessage(error: unknown, fallback: string) {
  return (
    (error as { response?: { data?: { detail?: string } } })?.response?.data
      ?.detail ||
    (error as Error)?.message ||
    fallback
  );
}

export function useCustomLLMs() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const query = useQuery<CustomLLMListResponse>({
    queryKey: customLLMsQueryKey,
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client.get("/custom-llms").then((response) => response.data);
    },
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: customLLMsQueryKey });
    queryClient.invalidateQueries({ queryKey: modelsQueryKey });
    queryClient.invalidateQueries({ queryKey: llmDiagnosticsQueryKey });
  };

  const createMutation = useMutation({
    mutationFn: async (request: CustomLLMCreateRequest) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .post("/custom-llms", request)
        .then((response) => response.data as CustomLLMResponse);
    },
    onSuccess: () => {
      invalidate();
      toast({ title: "Success", description: "Custom LLM added" });
    },
    onError: (error: unknown) => {
      toast({
        title: "Error",
        description: getErrorMessage(error, "Failed to add custom LLM"),
        variant: "destructive",
      });
    },
  });

  const updateMutation = useMutation({
    mutationFn: async ({
      id,
      request,
    }: {
      id: string;
      request: CustomLLMUpdateRequest;
    }) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .put(`/custom-llms/${id}`, request)
        .then((response) => response.data as CustomLLMResponse);
    },
    onSuccess: () => {
      invalidate();
      toast({ title: "Success", description: "Custom LLM updated" });
    },
    onError: (error: unknown) => {
      toast({
        title: "Error",
        description: getErrorMessage(error, "Failed to update custom LLM"),
        variant: "destructive",
      });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: async (customLLM: CustomLLM) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .delete(`/custom-llms/${customLLM.id}`)
        .then(
          (response) => response.data as ClearOrganizationAuthTokenResponse,
        );
    },
    onSuccess: () => {
      invalidate();
      toast({ title: "Success", description: "Custom LLM deleted" });
    },
    onError: (error: unknown) => {
      toast({
        title: "Error",
        description: getErrorMessage(error, "Failed to delete custom LLM"),
        variant: "destructive",
      });
    },
  });

  return {
    customLLMs: query.data?.custom_llms ?? [],
    isLoading: query.isLoading,
    createCustomLLM: createMutation.mutate,
    updateCustomLLM: updateMutation.mutate,
    deleteCustomLLM: deleteMutation.mutate,
    isCreating: createMutation.isPending,
    isUpdating: updateMutation.isPending,
    isDeleting: deleteMutation.isPending,
  };
}
