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
export { GOOGLE_SHEETS_REQUIRED_SCOPES } from "@/util/googleScopes";

const BROADCAST_CHANNEL_NAME = "skyvern:google-oauth-credentials";

// Shared across all consumers of the hook in a tab so posting from one
// component reaches listeners in other components without opening N channels.
const credentialBroadcastChannel: BroadcastChannel | null =
  typeof BroadcastChannel !== "undefined"
    ? new BroadcastChannel(BROADCAST_CHANNEL_NAME)
    : null;

// The channel can be disposed by the runtime (e.g. an embedded webview tearing
// down its native bridge during auth navigation) before this fires, in which
// case postMessage throws InvalidStateError. Cross-tab invalidation is
// best-effort, so swallow the failure rather than surfacing it.
export function safePostCredentialsInvalidate(
  channel: Pick<BroadcastChannel, "postMessage"> | null,
): void {
  try {
    channel?.postMessage("invalidate");
  } catch (error) {
    if (error instanceof DOMException && error.name === "InvalidStateError") {
      return;
    }
    throw error;
  }
}

function broadcastCredentialsChanged() {
  safePostCredentialsInvalidate(credentialBroadcastChannel);
}

export function normalizeGoogleOAuthScopes(
  scopes: readonly string[] | string | null | undefined,
): readonly string[] | undefined {
  if (Array.isArray(scopes)) {
    return scopes;
  }
  if (typeof scopes === "string") {
    return scopes
      .split(/[\s,]+/)
      .map((scope) => scope.trim())
      .filter(Boolean);
  }
  return undefined;
}

export function getGoogleOAuthCredentialScopesGranted(
  credential: GoogleOAuthCredential,
): readonly string[] {
  return (
    normalizeGoogleOAuthScopes(credential.scopes_granted) ??
    normalizeGoogleOAuthScopes(credential.scopes) ??
    []
  );
}

export function getGoogleOAuthCredentialScopesRequested(
  credential: GoogleOAuthCredential,
): readonly string[] {
  return normalizeGoogleOAuthScopes(credential.scopes_requested) ?? [];
}

export function isGoogleOAuthCredentialActive(
  credential: GoogleOAuthCredential,
): boolean {
  if (credential.state) {
    return credential.state === "active";
  }
  return credential.valid === true;
}

export function hasGoogleOAuthCredentialScopes(
  credential: GoogleOAuthCredential,
  requiredScopes: readonly string[],
): boolean {
  const granted = new Set(getGoogleOAuthCredentialScopesGranted(credential));
  return requiredScopes.every((scope) => granted.has(scope));
}

export function matchesGoogleOAuthIntegrationScopes(
  credential: GoogleOAuthCredential,
  requiredScopes: readonly string[],
): boolean {
  const requested = getGoogleOAuthCredentialScopesRequested(credential);
  if (requested.length > 0) {
    const requestedSet = new Set(requested);
    return requiredScopes.every((scope) => requestedSet.has(scope));
  }
  return hasGoogleOAuthCredentialScopes(credential, requiredScopes);
}

// Falls back to the first credential even when none are active, so a single
// needs-reconnect account is still selected rather than left blank.
export function getDefaultGoogleOAuthCredentialId(
  credentials: GoogleOAuthCredential[],
): string | undefined {
  return (
    credentials.find(isGoogleOAuthCredentialActive)?.id ?? credentials[0]?.id
  );
}

type ApiError = { response?: { data?: { detail?: string } } } & Error;

function extractApiErrorMessage(error: unknown, fallback: string): string {
  const err = error as ApiError | undefined;
  return err?.response?.data?.detail || err?.message || fallback;
}

export function useGoogleOAuthCredentials({
  enabled = true,
}: { enabled?: boolean } = {}) {
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
    enabled,
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
