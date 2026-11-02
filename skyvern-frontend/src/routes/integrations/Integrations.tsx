import { useState } from "react";
import {
  ExternalLinkIcon,
  ReloadIcon,
  TrashIcon,
  UpdateIcon,
} from "@radix-ui/react-icons";
import { GoogleOAuthCredential } from "@/api/types";
import { MessageIcon } from "@/components/icons/MessageIcon";
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
  DialogHeader,
  DialogFooter,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  googleCredentialGmailCapabilities,
  googleCredentialHasGmailCapability,
  hasGoogleOAuthCredentialScopes,
  isGoogleOAuthCredentialActive,
  useGoogleOAuthCredentials,
  type GmailCapability,
} from "@/hooks/useGoogleOAuthCredentials";
import { useTwilioCredentialQuery } from "@/hooks/useTwilioIntegration";
import {
  GOOGLE_DRIVE_REQUIRED_SCOPES,
  GOOGLE_GMAIL_REQUIRED_SCOPES,
  GOOGLE_SHEETS_BLOCK_REQUIRED_SCOPES,
  gmailScopeProfile,
} from "@/util/googleScopes";
import {
  GmailCapabilityCheckboxes,
  GmailCapabilityPills,
} from "./GmailCapabilities";
import {
  buildGoogleOAuthRedirectUri,
  getGoogleOAuthAppOrigin,
  storeGoogleOAuthIntegrationIdForState,
} from "./googleOAuth";
import { TwilioIntegrationPanel } from "./twilio/TwilioIntegrationPanel";

const integrations = [
  {
    id: "google_sheets",
    title: "Google Sheets",
    description: "Read, append, and update spreadsheet data from workflows.",
    scopeProfile: "google_sheets",
    capabilityScopes: GOOGLE_SHEETS_BLOCK_REQUIRED_SCOPES,
  },
  {
    id: "gmail",
    title: "Gmail",
    description: "Read verification codes and send email from your agents.",
    scopeProfile: "gmail",
    capabilityScopes: GOOGLE_GMAIL_REQUIRED_SCOPES,
  },
  {
    id: "google_drive",
    title: "Google Drive",
    description: "Upload generated files to connected Drive accounts.",
    scopeProfile: "google_drive",
    capabilityScopes: GOOGLE_DRIVE_REQUIRED_SCOPES,
  },
] as const;

function Integrations() {
  const [connectingId, setConnectingId] = useState<string | null>(null);
  const [reconnectingId, setReconnectingId] = useState<string | null>(null);
  const [enabling, setEnabling] = useState<{
    credentialId: string;
    capability: GmailCapability;
  } | null>(null);
  const [gmailCapabilities, setGmailCapabilities] = useState({
    read: true,
    send: false,
  });
  const newGmailScopeProfile = gmailScopeProfile(gmailCapabilities);
  const {
    credentials,
    isFetching,
    startAuthorize,
    isStartingAuthorize,
    deleteCredential,
    isDeletingCredential,
  } = useGoogleOAuthCredentials({ includeEmail: true });
  const {
    data: twilioCapability,
    isLoading: isLoadingTwilioCredential,
    isFetching: isFetchingTwilioCredential,
    isError: isTwilioCredentialError,
    error: twilioCredentialError,
    refetch: refetchTwilioCredential,
  } = useTwilioCredentialQuery();
  const twilioCredential = twilioCapability?.credential;
  const hasTwilioCredentialError =
    isTwilioCredentialError || Boolean(twilioCredentialError);

  const connect = async (integration: (typeof integrations)[number]) => {
    setConnectingId(integration.id);
    try {
      const response = await startAuthorize({
        redirect_uri: buildGoogleOAuthRedirectUri(),
        app_origin: getGoogleOAuthAppOrigin(),
        credential_name: integration.title,
        scope_profile:
          (integration.id === "gmail" ? newGmailScopeProfile : null) ??
          integration.scopeProfile,
      });
      storeGoogleOAuthIntegrationIdForState(response.state, integration.id);
      window.location.assign(response.authorize_url);
    } finally {
      setConnectingId(null);
    }
  };

  // Re-authenticate an existing connection in place. Passing credential_id keeps
  // the connection's identity so workflows referencing it keep working — the
  // backend re-requests the scopes originally granted to this credential.
  const reconnect = async (credential: GoogleOAuthCredential) => {
    setReconnectingId(credential.id);
    try {
      const response = await startAuthorize({
        redirect_uri: buildGoogleOAuthRedirectUri(),
        app_origin: getGoogleOAuthAppOrigin(),
        credential_id: credential.id,
      });
      window.location.assign(response.authorize_url);
    } finally {
      setReconnectingId(null);
    }
  };

  // Adds a Gmail permission to an existing connection. It keeps its id and current
  // access; if consent is cancelled or declined the connection is left as it was.
  const enableCapability = async (
    credential: GoogleOAuthCredential,
    capability: GmailCapability,
  ) => {
    setEnabling({ credentialId: credential.id, capability });
    try {
      const response = await startAuthorize({
        redirect_uri: buildGoogleOAuthRedirectUri(),
        app_origin: getGoogleOAuthAppOrigin(),
        credential_id: credential.id,
        scope_profile: capability === "send" ? "gmail_send" : "gmail",
      });
      storeGoogleOAuthIntegrationIdForState(response.state, "gmail");
      window.location.assign(response.authorize_url);
    } finally {
      setEnabling(null);
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

      <div className="grid gap-4 md:grid-cols-2">
        {integrations.map((integration) => {
          const isGmail = integration.id === "gmail";
          const matchingCredentials = credentials.filter((credential) => {
            if (isGmail) {
              const { read, send } =
                googleCredentialGmailCapabilities(credential);
              return read || send;
            }
            return hasGoogleOAuthCredentialScopes(
              credential,
              Array.from(integration.capabilityScopes),
            );
          });
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
                {isGmail ? (
                  <GmailCapabilityCheckboxes
                    value={gmailCapabilities}
                    onChange={setGmailCapabilities}
                  />
                ) : null}
                <Button
                  type="button"
                  className="w-full gap-2"
                  disabled={
                    isStartingAuthorize ||
                    (isGmail && newGmailScopeProfile === null)
                  }
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
      {twilioCapability?.available !== false ? (
        <section className="space-y-3">
          <div>
            <h2 className="text-lg font-semibold">Communication</h2>
            <p className="text-sm text-muted-foreground">
              Connect services that deliver verification messages to workflows.
            </p>
          </div>
          <div className="grid gap-4 md:grid-cols-3">
            <Card>
              <CardHeader>
                <div className="mb-2 flex size-9 items-center justify-center rounded-md border bg-muted">
                  <MessageIcon className="size-5" />
                </div>
                <CardTitle className="text-base">Twilio</CardTitle>
                <CardDescription>
                  Connected and enabled Twilio numbers deliver SMS 2FA codes
                  automatically. For any other number, push codes through the
                  2FA API or UI.
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="text-sm text-muted-foreground">
                  {isLoadingTwilioCredential
                    ? "Checking connection..."
                    : twilioCredential
                      ? "Connected"
                      : "Not connected"}
                </div>
                <Dialog>
                  <DialogTrigger asChild>
                    <Button
                      type="button"
                      className="w-full"
                      onClick={(event) =>
                        hasTwilioCredentialError &&
                        (event.preventDefault(), void refetchTwilioCredential())
                      }
                      disabled={
                        isLoadingTwilioCredential || isFetchingTwilioCredential
                      }
                    >
                      {(hasTwilioCredentialError && "Retry") ||
                        (twilioCredential ? "Manage" : "Configure")}
                    </Button>
                  </DialogTrigger>
                  <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-4xl">
                    <DialogHeader>
                      <DialogTitle>Twilio</DialogTitle>
                      <DialogDescription>
                        Connect Twilio and choose the phone numbers that deliver
                        SMS verification codes to Skyvern automatically.
                      </DialogDescription>
                    </DialogHeader>
                    <div className="pt-2">
                      <TwilioIntegrationPanel />
                    </div>
                  </DialogContent>
                </Dialog>
              </CardContent>
            </Card>
          </div>
        </section>
      ) : null}

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
              {credentials.map((credential) => {
                const isActive = isGoogleOAuthCredentialActive(credential);
                const isReconnecting = reconnectingId === credential.id;
                const showGmailPills =
                  isActive && googleCredentialHasGmailCapability(credential);
                return (
                  <div
                    key={credential.id}
                    className="flex items-center justify-between gap-4 p-4"
                  >
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium">
                        {credential.credential_name}
                      </div>
                      <div
                        className={
                          isActive
                            ? "text-xs text-muted-foreground"
                            : "text-xs font-medium text-amber-600 dark:text-amber-400"
                        }
                      >
                        {isActive ? "Active" : "Needs reconnect"}
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      {showGmailPills ? (
                        <GmailCapabilityPills
                          credential={credential}
                          pendingAction={
                            enabling?.credentialId === credential.id
                              ? enabling.capability
                              : null
                          }
                          disabled={isStartingAuthorize}
                          onEnable={(capability) =>
                            void enableCapability(credential, capability)
                          }
                        />
                      ) : null}
                      <Button
                        type="button"
                        variant={isActive ? "outline" : "default"}
                        size="sm"
                        className="gap-2"
                        disabled={isReconnecting || isStartingAuthorize}
                        onClick={() => void reconnect(credential)}
                      >
                        {isReconnecting ? (
                          <ReloadIcon className="h-4 w-4 animate-spin" />
                        ) : (
                          <UpdateIcon className="h-4 w-4" />
                        )}
                        Reconnect
                      </Button>
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
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

export { Integrations };
