import { type FormEvent, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLinkIcon } from "@radix-ui/react-icons";
import { getClient } from "@/api/AxiosClient";
import {
  GoogleOAuthClientConfigResponse,
  UpdateGoogleOAuthClientConfigRequest,
} from "@/api/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useToast } from "@/components/ui/use-toast";
import { ClearCredentialDialog } from "@/components/ClearCredentialDialog";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { buildGoogleOAuthRedirectUri } from "@/routes/integrations/googleOAuth";

const queryKey = ["googleOAuthClientConfig"];

function splitList(value: string): string[] {
  return value
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function joinList(values: string[] | undefined, fallback: string): string {
  return values && values.length > 0 ? values.join("\n") : fallback;
}

function apiErrorMessage(error: unknown, fallback: string): string {
  return (
    (error as { response?: { data?: { detail?: string } } })?.response?.data
      ?.detail ||
    (error as Error)?.message ||
    fallback
  );
}

type Props = {
  onSuccess?: () => void;
};

export function GoogleOAuthClientConfigForm({ onSuccess }: Props = {}) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const callbackUrl = useMemo(() => buildGoogleOAuthRedirectUri(), []);
  const defaultRedirectHost =
    typeof window !== "undefined" ? window.location.hostname : "localhost";
  const defaultAppOrigin =
    typeof window !== "undefined" ? window.location.origin : "";

  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [redirectHosts, setRedirectHosts] = useState(defaultRedirectHost);
  const [appOrigins, setAppOrigins] = useState(defaultAppOrigin);

  const { data, isLoading } = useQuery({
    queryKey,
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get("/google/oauth/config")
        .then((response) => response.data as GoogleOAuthClientConfigResponse);
    },
  });
  const config = data?.config;

  useEffect(() => {
    setClientId(config?.client_id ?? "");
    setClientSecret("");
    setRedirectHosts(
      config?.configured
        ? (config.redirect_hosts ?? []).join("\n")
        : joinList(config?.redirect_hosts, defaultRedirectHost),
    );
    setAppOrigins(
      config?.configured
        ? (config.app_origins ?? []).join("\n")
        : joinList(config?.app_origins, defaultAppOrigin),
    );
  }, [config, defaultAppOrigin, defaultRedirectHost]);

  const saveMutation = useMutation({
    mutationFn: async (request: UpdateGoogleOAuthClientConfigRequest) => {
      const client = await getClient(credentialGetter);
      return client
        .put("/google/oauth/config", request)
        .then((response) => response.data as GoogleOAuthClientConfigResponse);
    },
    onSuccess: () => {
      setClientSecret("");
      queryClient.invalidateQueries({ queryKey });
      toast({
        title: "Success",
        description: "Google OAuth configuration saved",
      });
      onSuccess?.();
    },
    onError: (error) => {
      toast({
        title: "Error",
        description: apiErrorMessage(
          error,
          "Failed to save Google OAuth configuration",
        ),
        variant: "destructive",
      });
    },
  });

  const clearMutation = useMutation({
    mutationFn: async () => {
      const client = await getClient(credentialGetter);
      return client.delete("/google/oauth/config");
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
      toast({
        title: "Success",
        description: "Google OAuth configuration cleared",
      });
    },
    onError: (error) => {
      toast({
        title: "Error",
        description: apiErrorMessage(
          error,
          "Failed to clear Google OAuth configuration",
        ),
        variant: "destructive",
      });
    },
  });

  const isMutating = saveMutation.isPending || clearMutation.isPending;
  const encryptionEnabled = config?.encryption_enabled ?? false;
  const canClear = config?.source === "organization";

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    saveMutation.mutate({
      client_id: clientId,
      client_secret: clientSecret || undefined,
      redirect_hosts: splitList(redirectHosts),
      app_origins: splitList(appOrigins),
    });
  };

  return (
    <form onSubmit={onSubmit} className="space-y-5">
      <div className="space-y-2">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h3 className="text-lg font-medium">Google OAuth</h3>
            <p className="text-sm text-muted-foreground">
              Configure the OAuth client used for Google Sheets, Gmail, and
              Drive.
            </p>
          </div>
          <a
            href="https://console.cloud.google.com/apis/credentials"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-2 text-sm text-blue-600 underline"
          >
            Google Cloud Console
            <ExternalLinkIcon className="h-3.5 w-3.5" />
          </a>
        </div>
        {!encryptionEnabled && (
          <div className="rounded-md border border-yellow-300 bg-yellow-50 px-3 py-2 text-sm text-yellow-900">
            Enable AES encryption before saving or connecting Google accounts.
          </div>
        )}
        <div className="rounded-md bg-muted px-3 py-2 text-sm">
          <div className="mb-1 text-muted-foreground">Callback URL</div>
          <Input readOnly value={callbackUrl} className="bg-background" />
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="google-client-id">Client ID</Label>
          <Input
            id="google-client-id"
            value={clientId}
            onChange={(event) => setClientId(event.target.value)}
            disabled={isLoading || isMutating}
            placeholder="123.apps.googleusercontent.com"
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="google-client-secret">Client Secret</Label>
          <Input
            id="google-client-secret"
            type="password"
            value={clientSecret}
            onChange={(event) => setClientSecret(event.target.value)}
            disabled={isLoading || isMutating}
            placeholder={
              config?.client_secret_configured &&
              config?.source === "organization"
                ? "Leave blank to keep existing secret"
                : "Client secret"
            }
          />
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="google-redirect-hosts">Redirect Hosts</Label>
          <Textarea
            id="google-redirect-hosts"
            value={redirectHosts}
            onChange={(event) => setRedirectHosts(event.target.value)}
            disabled={isLoading || isMutating}
            className="min-h-24"
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="google-app-origins">App Origins</Label>
          <Textarea
            id="google-app-origins"
            value={appOrigins}
            onChange={(event) => setAppOrigins(event.target.value)}
            disabled={isLoading || isMutating}
            className="min-h-24"
          />
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button
          type="submit"
          disabled={isLoading || isMutating || !encryptionEnabled}
        >
          {saveMutation.isPending ? "Saving..." : "Save Configuration"}
        </Button>
        {canClear && (
          <ClearCredentialDialog
            label="Clear Configuration"
            title="Clear Google OAuth configuration?"
            description="Google OAuth will fall back to environment configuration if it is available."
            disabled={isLoading || isMutating}
            isPending={clearMutation.isPending}
            onConfirm={() => clearMutation.mutate()}
          />
        )}
        <span className="text-sm text-muted-foreground">
          Status:{" "}
          {config?.configured
            ? `Configured via ${config.source}`
            : "Not configured"}
        </span>
      </div>
    </form>
  );
}
