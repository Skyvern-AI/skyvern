import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { toast } from "@/components/ui/use-toast";
import { useMutation, useQueryClient } from "@tanstack/react-query";

function useDeleteBrowserProfileMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (profileId: string) => {
      const client = await getClient(credentialGetter);
      await client.delete(`/browser_profiles/${profileId}`);
      return profileId;
    },
    onSuccess: (profileId) => {
      queryClient.invalidateQueries({ queryKey: ["browserProfiles"] });
      queryClient.invalidateQueries({
        queryKey: ["browserProfile", profileId],
      });
      toast({
        title: "Browser profile deleted",
        description: "The browser profile has been removed.",
      });
    },
    onError: (error: unknown) => {
      const message =
        (error as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ||
        (error as Error)?.message ||
        "Failed to delete browser profile";

      toast({
        title: "Failed to delete browser profile",
        description: message,
        variant: "destructive",
      });
    },
  });
}

export { useDeleteBrowserProfileMutation };
