import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "./useCredentialGetter";
import {
  CreateMicrosoftOAuthAuthorizeRequest,
  CreateMicrosoftOAuthCallbackRequest,
  MicrosoftOAuthAuthorizeResponse,
  MicrosoftOAuthCredential,
  MicrosoftOAuthCredentialListResponse,
  MicrosoftOAuthCredentialResponse,
} from "@/api/types";
import { useToast } from "@/components/ui/use-toast";
export { MICROSOFT_MAIL_REQUIRED_SCOPES } from "@/util/microsoftScopes";

const BROADCAST_CHANNEL_NAME = "skyvern:microsoft-oauth-credentials";

const credentialBroadcastChannel: BroadcastChannel | null =
  typeof BroadcastChannel !== "undefined"
    ? new BroadcastChannel(BROADCAST_CHANNEL_NAME)
    : null;

function safePostCredentialsInvalidate(
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

export function normalizeMicrosoftOAuthScopes(
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

export function normalizeMicrosoftScopeSegment(scope: string): string {
  const parts = scope.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? scope;
}

export function getMicrosoftOAuthCredentialScopesGranted(
  credential: MicrosoftOAuthCredential,
): readonly string[] {
  return (
    normalizeMicrosoftOAuthScopes(credential.scopes_granted) ??
    normalizeMicrosoftOAuthScopes(credential.scopes) ??
    []
  );
}

export function getMicrosoftOAuthCredentialScopesRequested(
  credential: MicrosoftOAuthCredential,
): readonly string[] {
  return normalizeMicrosoftOAuthScopes(credential.scopes_requested) ?? [];
}

export function isMicrosoftOAuthCredentialActive(
  credential: MicrosoftOAuthCredential,
): boolean {
  if (credential.state) {
    return credential.state === "active";
  }
  return credential.valid === true;
}

export function hasMicrosoftOAuthCredentialScopes(
  credential: MicrosoftOAuthCredential,
  requiredScopes: readonly string[],
): boolean {
  const granted = new Set(
    getMicrosoftOAuthCredentialScopesGranted(credential).map(
      normalizeMicrosoftScopeSegment,
    ),
  );
  return requiredScopes.every((scope) =>
    granted.has(normalizeMicrosoftScopeSegment(scope)),
  );
}

export function matchesMicrosoftOAuthIntegrationScopes(
  credential: MicrosoftOAuthCredential,
  requiredScopes: readonly string[],
): boolean {
  const requested = getMicrosoftOAuthCredentialScopesRequested(credential);
  if (requested.length > 0) {
    const requestedSet = new Set(requested.map(normalizeMicrosoftScopeSegment));
    return requiredScopes.every((scope) =>
      requestedSet.has(normalizeMicrosoftScopeSegment(scope)),
    );
  }
  return hasMicrosoftOAuthCredentialScopes(credential, requiredScopes);
}

export function getDefaultMicrosoftOAuthCredentialId(
  credentials: MicrosoftOAuthCredential[],
): string | undefined {
  return (
    credentials.find(isMicrosoftOAuthCredentialActive)?.id ?? credentials[0]?.id
  );
}

type ApiError = { response?: { data?: { detail?: string } } } & Error;

function extractApiErrorMessage(error: unknown, fallback: string): string {
  const err = error as ApiError | undefined;
  return err?.response?.data?.detail || err?.message || fallback;
}

export function useMicrosoftOAuthCredentials({
  enabled = true,
}: { enabled?: boolean } = {}) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { toast } = useToast();

  useEffect(() => {
    if (!credentialBroadcastChannel) return;
    const listener = () => {
      queryClient.invalidateQueries({
        queryKey: ["microsoftOAuthCredentials"],
      });
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
  } = useQuery<MicrosoftOAuthCredential[]>({
    queryKey: ["microsoftOAuthCredentials"],
    enabled,
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.get("/microsoft/oauth/credentials");
      return (response.data as MicrosoftOAuthCredentialListResponse)
        .credentials;
    },
    staleTime: 30_000,
    refetchOnWindowFocus: true,
  });

  const authorizeMutation = useMutation({
    mutationFn: async (data: CreateMicrosoftOAuthAuthorizeRequest) => {
      const client = await getClient(credentialGetter);
      return await client
        .post("/microsoft/oauth/authorize", data)
        .then((response) => response.data as MicrosoftOAuthAuthorizeResponse);
    },
    onError: (error: unknown) => {
      toast({
        title: "Error",
        description: extractApiErrorMessage(
          error,
          "Failed to start Microsoft OAuth flow",
        ),
        variant: "destructive",
      });
    },
  });

  const oauthCallbackMutation = useMutation({
    mutationFn: async (data: CreateMicrosoftOAuthCallbackRequest) => {
      const client = await getClient(credentialGetter);
      return await client
        .post("/microsoft/oauth/callback", data)
        .then((response) => response.data as MicrosoftOAuthCredentialResponse);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["microsoftOAuthCredentials"],
      });
      broadcastCredentialsChanged();
      toast({
        title: "Success",
        description: "Microsoft account connected successfully",
      });
    },
    onError: (error: unknown) => {
      toast({
        title: "Error",
        description: extractApiErrorMessage(
          error,
          "Failed to connect Microsoft account",
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
        .patch(`/microsoft/oauth/credentials/${input.credentialId}`, {
          credential_name: input.credentialName,
        })
        .then((response) => response.data as MicrosoftOAuthCredentialResponse);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["microsoftOAuthCredentials"],
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
      return await client.delete(
        `/microsoft/oauth/credentials/${credentialId}`,
      );
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["microsoftOAuthCredentials"],
      });
      broadcastCredentialsChanged();
      toast({
        title: "Success",
        description: "Microsoft credential disconnected",
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
