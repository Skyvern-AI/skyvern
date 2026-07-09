import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";

import { getClient } from "@/api/AxiosClient";
import { BrowserProfileApiResponse, ProxyLocation } from "@/api/types";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

type UpdateBrowserProfileInput = {
  profileId: string;
  name?: string;
  description?: string | null;
  proxy_location?: ProxyLocation | null;
  proxy_session_id?: string | null;
  rotate_proxy_session_id?: boolean;
};

function useUpdateBrowserProfileMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({
      profileId,
      name,
      description,
      proxy_location,
      proxy_session_id,
      rotate_proxy_session_id,
    }: UpdateBrowserProfileInput) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const payload: Record<string, string | boolean | ProxyLocation | null> =
        {};
      if (name !== undefined) {
        payload.name = name;
      }
      if (description !== undefined) {
        payload.description = description;
      }
      if (proxy_location !== undefined) {
        payload.proxy_location = proxy_location;
      }
      if (proxy_session_id !== undefined) {
        payload.proxy_session_id = proxy_session_id;
      }
      if (rotate_proxy_session_id !== undefined) {
        payload.rotate_proxy_session_id = rotate_proxy_session_id;
      }
      return client
        .patch<BrowserProfileApiResponse>(
          `/browser_profiles/${profileId}`,
          payload,
        )
        .then((response) => response.data);
    },
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ["browserProfiles"] });
      queryClient.invalidateQueries({
        queryKey: ["browserProfile", variables.profileId],
      });
      toast({
        title: "Browser profile updated",
        variant: "success",
        description: "The browser profile has been updated.",
      });
    },
    onError: (error: AxiosError) => {
      const detail =
        (error.response?.data as { detail?: string } | undefined)?.detail ??
        error.message;
      toast({
        variant: "destructive",
        title: "Failed to update browser profile",
        description: detail,
      });
    },
  });
}

function useDeleteBrowserProfileMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (profileId: string) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client.delete(`/browser_profiles/${profileId}`);
    },
    onSuccess: (_data, profileId) => {
      queryClient.invalidateQueries({ queryKey: ["browserProfiles"] });
      queryClient.invalidateQueries({
        queryKey: ["browserProfile", profileId],
      });
      toast({
        title: "Browser profile deleted",
        variant: "success",
        description: "The browser profile has been deleted.",
      });
    },
    onError: (error: AxiosError) => {
      const detail =
        (error.response?.data as { detail?: string } | undefined)?.detail ??
        error.message;
      toast({
        variant: "destructive",
        title: "Failed to delete browser profile",
        description: detail,
      });
    },
  });
}

export { useUpdateBrowserProfileMutation, useDeleteBrowserProfileMutation };
