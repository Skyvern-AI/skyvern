import { useState } from "react";
import { ExternalLinkIcon, ReloadIcon, TrashIcon } from "@radix-ui/react-icons";
import { GoogleOAuthClientConfigForm } from "@/components/GoogleOAuthClientConfigForm";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  hasGoogleOAuthCredentialScopes,
  isGoogleOAuthCredentialActive,
  useGoogleOAuthCredentials,
} from "@/hooks/useGoogleOAuthCredentials";
import {
  GOOGLE_DRIVE_REQUIRED_SCOPES,
  GOOGLE_GMAIL_REQUIRED_SCOPES,
  GOOGLE_SHEETS_REQUIRED_SCOPES,
} from "@/util/googleScopes";
import {
  buildGoogleOAuthRedirectUri,
  getGoogleOAuthAppOrigin,
  storeGoogleOAuthIntegrationIdForState,
} from "./googleOAuth";

const integrations = [
  {
    id: "google_sheets",
    title: "Google Sheets",
    description: "Read, append, and update spreadsheet data from workflows.",
    scopeProfile: "google_sheets",
    requiredScopes: GOOGLE_SHEETS_REQUIRED_SCOPES,
  },
  {
    id: "gmail",
    title: "Gmail",
    description: "Read verification emails for OTP polling.",
    scopeProfile: "gmail",
    requiredScopes: GOOGLE_GMAIL_REQUIRED_SCOPES,
  },
  {
    id: "google_drive",
    title: "Google Drive",
    description: "Upload generated files to connected Drive accounts.",
    scopeProfile: "google_drive",
    requiredScopes: GOOGLE_DRIVE_REQUIRED_SCOPES,
  },
] as const;

function Integrations() {
  const [connectingId, setConnectingId] = useState<string | null>(null);
  const {
    credentials,
    isFetching,
    startAuthorize,
    isStartingAuthorize,
    deleteCredential,
    isDeletingCredential,
  } = useGoogleOAuthCredentials();

  const connect = async (integration: (typeof integrations)[number]) => {
    setConnectingId(integration.id);
    try {
      const response = await startAuthorize({
        redirect_uri: buildGoogleOAuthRedirectUri(),
        app_origin: getGoogleOAuthAppOrigin(),
        credential_name: integration.title,
        scope_profile: integration.scopeProfile,
      });
      storeGoogleOAuthIntegrationIdForState(response.state, integration.id);
      window.location.assign(response.authorize_url);
    } finally {
      setConnectingId(null);
    }
  };

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6 p-6">
      <Card>
        <CardHeader className="border-b-2">
          <CardTitle className="text-lg">Google Integrations</CardTitle>
          <CardDescription>
            Connect Google accounts for Sheets, Gmail, and Drive workflows.
          </CardDescription>
        </CardHeader>
        <CardContent className="p-6">
          <GoogleOAuthClientConfigForm />
        </CardContent>
      </Card>

      <div className="grid gap-4 md:grid-cols-3">
        {integrations.map((integration) => {
          const matchingCredentials = credentials.filter((credential) =>
            hasGoogleOAuthCredentialScopes(
              credential,
              Array.from(integration.requiredScopes),
            ),
          );
          const activeCount = matchingCredentials.filter(
            isGoogleOAuthCredentialActive,
          ).length;
          return (
            <Card key={integration.id}>
              <CardHeader>
                <CardTitle className="text-base">{integration.title}</CardTitle>
                <CardDescription>{integration.description}</CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="text-sm text-muted-foreground">
                  {activeCount} active connection{activeCount === 1 ? "" : "s"}
                </div>
                <Button
                  type="button"
                  className="w-full gap-2"
                  disabled={isStartingAuthorize}
                  onClick={() => void connect(integration)}
                >
                  {connectingId === integration.id ? (
                    <ReloadIcon className="h-4 w-4 animate-spin" />
                  ) : (
                    <ExternalLinkIcon className="h-4 w-4" />
                  )}
                  Connect
                </Button>
              </CardContent>
            </Card>
          );
        })}
      </div>

      <Card>
        <CardHeader className="border-b-2">
          <CardTitle className="text-lg">Connected Google Accounts</CardTitle>
          <CardDescription>
            Accounts listed here are available to Google workflow blocks.
          </CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          {credentials.length === 0 ? (
            <div className="p-6 text-sm text-muted-foreground">
              {isFetching
                ? "Loading connections..."
                : "No Google accounts connected"}
            </div>
          ) : (
            <div className="divide-y">
              {credentials.map((credential) => (
                <div
                  key={credential.id}
                  className="flex items-center justify-between gap-4 p-4"
                >
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium">
                      {credential.credential_name}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {isGoogleOAuthCredentialActive(credential)
                        ? "Active"
                        : "Needs reconnect"}
                    </div>
                  </div>
                  <Dialog>
                    <DialogTrigger asChild>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="gap-2"
                        disabled={isDeletingCredential}
                      >
                        <TrashIcon className="h-4 w-4" />
                        Disconnect
                      </Button>
                    </DialogTrigger>
                    <DialogContent>
                      <DialogTitle>Disconnect Google account?</DialogTitle>
                      <DialogDescription>
                        Workflows using{" "}
                        <span className="font-medium text-foreground">
                          {credential.credential_name}
                        </span>{" "}
                        will lose access to this Google account.
                      </DialogDescription>
                      <DialogFooter>
                        <DialogClose asChild>
                          <Button
                            type="button"
                            variant="secondary"
                            disabled={isDeletingCredential}
                          >
                            Cancel
                          </Button>
                        </DialogClose>
                        <DialogClose asChild>
                          <Button
                            type="button"
                            variant="destructive"
                            disabled={isDeletingCredential}
                            onClick={() => deleteCredential(credential.id)}
                          >
                            Disconnect
                          </Button>
                        </DialogClose>
                      </DialogFooter>
                    </DialogContent>
                  </Dialog>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

export { Integrations };
