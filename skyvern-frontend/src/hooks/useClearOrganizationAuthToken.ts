import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ClearOrganizationAuthTokenResponse } from "@/api/types";
import { getClient } from "@/api/AxiosClient";
import { useToast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "./useCredentialGetter";

type ClearOrganizationAuthTokenOptions = {
  providerPath: string;
  queryKey: string;
  invalidateQueryKeys?: string[];
  successDescription: string;
  errorDescription: string;
};

export function useClearOrganizationAuthToken({
  providerPath,
  queryKey,
  invalidateQueryKeys,
  successDescription,
  errorDescription,
}: ClearOrganizationAuthTokenOptions) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { toast } = useToast();

  return useMutation({
    mutationFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return await client
        .delete(`/credentials/${providerPath}`)
        .then(
          (response) => response.data as ClearOrganizationAuthTokenResponse,
        );
    },
    onSuccess: () => {
      queryClient.setQueryData([queryKey], null);
      queryClient.invalidateQueries({ queryKey: [queryKey] });
      (invalidateQueryKeys ?? []).forEach((key) => {
        queryClient.invalidateQueries({ queryKey: [key] });
      });
      toast({
        title: "Success",
        description: successDescription,
      });
    },
    onError: (error: unknown) => {
      const message =
        (error as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ||
        (error as Error)?.message ||
        errorDescription;
      toast({
        title: "Error",
        description: message,
        variant: "destructive",
      });
    },
  });
}
