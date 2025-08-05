import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "./useCredentialGetter";
import {
  CreateOnePasswordTokenRequest,
  CreateOnePasswordTokenResponse,
  OnePasswordTokenApiResponse,
} from "@/api/types";
import { useToast } from "@/components/ui/use-toast";

export function useOnePasswordToken() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const { data: onePasswordToken, isLoading } =
    useQuery<OnePasswordTokenApiResponse>({
      queryKey: ["onePasswordToken"],
      queryFn: async () => {
        const client = await getClient(credentialGetter);
        return await client
          .get("/auth-tokens/onepassword")
          .then((response) => response.data.token)
          .catch(() => null);
      },
    });

  const createOrUpdateTokenMutation = useMutation({
    mutationFn: async (data: CreateOnePasswordTokenRequest) => {
      const client = await getClient(credentialGetter);
      return await client
        .post("/auth-tokens/onepassword", data)
        .then((response) => response.data as CreateOnePasswordTokenResponse);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["onePasswordToken"] });
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

  return {
    onePasswordToken,
    isLoading,
    createOrUpdateToken: createOrUpdateTokenMutation.mutate,
    isUpdating: createOrUpdateTokenMutation.isPending,
  };
}
