import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "./useCredentialGetter";
import {
  CreateGoogleOAuthAuthorizeRequest,
  GoogleOAuthAuthorizeResponse,
  GoogleOAuthCredential,
  GoogleOAuthCredentialListResponse,
  GoogleOAuthCredentialResponse,
} from "@/api/types";
import { useToast } from "@/components/ui/use-toast";

const BROADCAST_CHANNEL_NAME = "skyvern:google-oauth-credentials";

// Shared across all consumers of the hook in a tab so posting from one
// component reaches listeners in other components without opening N channels.
const credentialBroadcastChannel: BroadcastChannel | null =
  typeof BroadcastChannel !== "undefined"
    ? new BroadcastChannel(BROADCAST_CHANNEL_NAME)
    : null;

function broadcastCredentialsChanged() {
  credentialBroadcastChannel?.postMessage("invalidate");
}

type ApiError = { response?: { data?: { detail?: string } } } & Error;

function extractApiErrorMessage(error: unknown, fallback: string): string {
  const err = error as ApiError | undefined;
  return err?.response?.data?.detail || err?.message || fallback;
}

export function useGoogleOAuthCredentials() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { toast } = useToast();

  useEffect(() => {
    if (!credentialBroadcastChannel) return;
    const listener = () => {
      queryClient.invalidateQueries({ queryKey: ["googleOAuthCredentials"] });
    };
    credentialBroadcastChannel.addEventListener("message", listener);
    return () => {
      credentialBroadcastChannel.removeEventListener("message", listener);
    };
  }, [queryClient]);

  const {
    data: credentials,
    isLoading,
    isFetching,
    error,
  } = useQuery<GoogleOAuthCredential[]>({
    queryKey: ["googleOAuthCredentials"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.get("/google/oauth/credentials");
      return (response.data as GoogleOAuthCredentialListResponse).credentials;
    },
    // Moderate staleness so multiple GoogleOAuthCredentialSelector instances
    // (one per Google Sheets block in a workflow) don't all refetch on every
    // window focus. The integrations directory and the OAuth callback path
    // explicitly invalidate this query, so user-visible updates remain prompt.
    staleTime: 30_000,
    refetchOnWindowFocus: true,
  });

  const authorizeMutation = useMutation({
    mutationFn: async (data: CreateGoogleOAuthAuthorizeRequest) => {
      const client = await getClient(credentialGetter);
      return await client
        .post("/google/oauth/authorize", data)
        .then((response) => response.data as GoogleOAuthAuthorizeResponse);
    },
    onError: (error: unknown) => {
      toast({
        title: "Error",
        description: extractApiErrorMessage(
          error,
          "Failed to start Google OAuth flow",
        ),
        variant: "destructive",
      });
    },
  });

  const oauthCallbackMutation = useMutation({
    mutationFn: async (data: { code: string; state: string }) => {
      const client = await getClient(credentialGetter);
      return await client
        .post("/google/oauth/callback", data)
        .then((response) => response.data as GoogleOAuthCredentialResponse);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["googleOAuthCredentials"],
      });
      broadcastCredentialsChanged();
      toast({
        title: "Success",
        description: "Google account connected successfully",
      });
    },
    onError: (error: unknown) => {
      toast({
        title: "Error",
        description: extractApiErrorMessage(
          error,
          "Failed to connect Google account",
        ),
        variant: "destructive",
      });
    },
  });

  const renameCredentialMutation = useMutation({
    mutationFn: async (input: {
      credentialId: string;
      credentialName: string;
    }) => {
      const client = await getClient(credentialGetter);
      return await client
        .patch(`/google/oauth/credentials/${input.credentialId}`, {
          credential_name: input.credentialName,
        })
        .then((response) => response.data as GoogleOAuthCredentialResponse);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["googleOAuthCredentials"],
      });
      broadcastCredentialsChanged();
      toast({
        title: "Success",
        description: "Connection renamed",
      });
    },
    onError: (error: unknown) => {
      toast({
        title: "Error",
        description: extractApiErrorMessage(
          error,
          "Failed to rename credential",
        ),
        variant: "destructive",
      });
    },
  });

  const deleteCredentialMutation = useMutation({
    mutationFn: async (credentialId: string) => {
      const client = await getClient(credentialGetter);
      return await client.delete(`/google/oauth/credentials/${credentialId}`);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["googleOAuthCredentials"],
      });
      broadcastCredentialsChanged();
      toast({
        title: "Success",
        description: "Google credential disconnected",
      });
    },
    onError: (error: unknown) => {
      toast({
        title: "Error",
        description: extractApiErrorMessage(
          error,
          "Failed to disconnect credential",
        ),
        variant: "destructive",
      });
    },
  });

  return {
    credentials: credentials ?? [],
    isLoading,
    isFetching,
    error,
    startAuthorize: authorizeMutation.mutateAsync,
    isStartingAuthorize: authorizeMutation.isPending,
    submitOAuthCallback: oauthCallbackMutation.mutate,
    submitOAuthCallbackAsync: oauthCallbackMutation.mutateAsync,
    isSubmittingCallback: oauthCallbackMutation.isPending,
    deleteCredential: deleteCredentialMutation.mutate,
    deleteCredentialAsync: deleteCredentialMutation.mutateAsync,
    isDeletingCredential: deleteCredentialMutation.isPending,
    renameCredentialAsync: renameCredentialMutation.mutateAsync,
    isRenamingCredential: renameCredentialMutation.isPending,
  };
}
