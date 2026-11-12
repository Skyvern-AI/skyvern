import { useEffect, useRef, useState } from "react";

import type {
  GoogleOAuthCredential,
  GoogleOAuthScopeProfile,
} from "@/api/types";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  hasGoogleOAuthCredentialScopes,
  isGoogleOAuthCredentialActive,
  useGoogleOAuthCredentials,
} from "@/hooks/useGoogleOAuthCredentials";
import {
  hasMicrosoftOAuthCredentialScopes,
  isMicrosoftOAuthCredentialActive,
  useMicrosoftOAuthCredentials,
} from "@/hooks/useMicrosoftOAuthCredentials";
import {
  buildGoogleOAuthRedirectUri,
  getGoogleOAuthAppOrigin,
  storeGoogleOAuthIntegrationIdForState,
} from "@/routes/integrations/googleOAuth";
import { markGoogleOAuthPopup } from "@/routes/integrations/googleOAuthPopup";
import { useHasCapability } from "@/store/PageSlots";

import {
  connectionCardKey,
  type ConnectionCard as ConnectionCardData,
} from "../narrativeState";
import type { ConnectedAccountChoice } from "../workflowCopilotTypes";
import { ConnectedAccountChoiceCard } from "./ConnectedAccountChoiceCard";
import { googleConnectionChanged } from "./googleConnectionChanged";

const GOOGLE_SCOPE_PROFILES: readonly GoogleOAuthScopeProfile[] = [
  "google_sheets",
  "gmail",
  "google_drive",
  "gmail_send",
  "gmail_read_send",
];

const POPUP_POLL_MS = 500;

type AttemptState =
  | "idle"
  | "open"
  | "not_completed"
  | "popup_blocked"
  | "forbidden"
  | "start_failed";

const ATTEMPT_FAILURES: Partial<Record<AttemptState, string>> = {
  popup_blocked:
    "Your browser blocked the sign-in window. Allow pop-ups for this site, then try again.",
  forbidden: "Organization admin access is required to connect an account.",
  start_failed: "Could not start the connection.",
};

function responseStatus(error: unknown): number | undefined {
  return (error as { response?: { status?: number } } | null)?.response?.status;
}

function googleScopeProfile(
  value: string | null,
): GoogleOAuthScopeProfile | undefined {
  return GOOGLE_SCOPE_PROFILES.find((profile) => profile === value);
}

export function ConnectionCard({
  card,
  selectedConnectionId = null,
  disabled = false,
  onSelect,
}: {
  card: ConnectionCardData;
  selectedConnectionId?: string | null;
  disabled?: boolean;
  onSelect?: (connectionId: string) => void;
}) {
  const isGoogle = card.provider === "google";
  const accountKind = isGoogle ? "Google" : card.integrationName;
  const google = useGoogleOAuthCredentials({
    enabled: isGoogle,
    refetchOnMount: "always",
  });
  const microsoft = useMicrosoftOAuthCredentials({
    enabled: !isGoogle,
    includeEmail: true,
  });
  const canGoogleAuthorize = useHasCapability("google_oauth.authorize");
  const canMicrosoftAuthorize = useHasCapability("microsoft_oauth.authorize");
  const canAuthorize = isGoogle ? canGoogleAuthorize : canMicrosoftAuthorize;
  const [isStarting, setIsStarting] = useState(false);
  const [confirmSetAccount, setConfirmSetAccount] = useState(false);
  const [attempt, setAttempt] = useState<AttemptState>("idle");
  const popupRef = useRef<Window | null>(null);
  const snapshotRef = useRef<GoogleOAuthCredential[]>([]);
  const refetchGoogle = google.refetchCredentials;
  const reconnectConnectionId = card.reconnectConnectionId;
  const [initialIds, setInitialIds] = useState<Set<string> | null>(null);
  const credentialsLoading = isGoogle ? google.isLoading : microsoft.isLoading;
  const loadedIds = (isGoogle ? google.credentials : microsoft.credentials).map(
    (credential) => credential.id,
  );
  const loadedIdsKey = loadedIds.join(" ");

  useEffect(() => {
    if (initialIds === null && !credentialsLoading) {
      setInitialIds(new Set(loadedIdsKey.split(" ").filter(Boolean)));
    }
  }, [initialIds, credentialsLoading, loadedIdsKey]);

  useEffect(() => {
    if (attempt !== "open") return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      if (!popupRef.current?.closed) return;
      window.clearInterval(timer);
      popupRef.current = null;
      void refetchGoogle()
        .then(({ data }) =>
          googleConnectionChanged(
            snapshotRef.current,
            data ?? [],
            reconnectConnectionId,
          ),
        )
        .catch(() => false)
        .then((changed) => {
          if (!cancelled) setAttempt(changed ? "idle" : "not_completed");
        });
    }, POPUP_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [attempt, refetchGoogle, reconnectConnectionId]);

  const choices: ConnectedAccountChoice[] = isGoogle
    ? google.credentials
        .filter(
          (credential) =>
            isGoogleOAuthCredentialActive(credential) &&
            hasGoogleOAuthCredentialScopes(credential, card.requiredScopes),
        )
        .map((credential) => ({
          connection_id: credential.id,
          name: credential.credential_name,
          state: "active",
          email_address: credential.email_address ?? null,
        }))
    : microsoft.credentials
        .filter(
          (credential) =>
            isMicrosoftOAuthCredentialActive(credential) &&
            hasMicrosoftOAuthCredentialScopes(credential, card.requiredScopes),
        )
        .map((credential) => ({
          connection_id: credential.id,
          name: credential.credential_name,
          state: "active",
          email_address: credential.email_address ?? null,
        }));

  const reconnectCredential = google.credentials.find(
    (credential) => credential.id === card.reconnectConnectionId,
  );

  const startGoogle = async (setAccount = false) => {
    if (!setAccount && reconnectCredential?.account_recorded === false) {
      setConfirmSetAccount(true);
      return;
    }
    snapshotRef.current = google.credentials;
    // One named window per card, so starting a sibling card's consent cannot take over this card's popup.
    const authTab = window.open(
      "",
      `skyvern-oauth-${connectionCardKey(card)}`,
      "popup,width=600,height=760",
    );
    if (!authTab) {
      setAttempt("popup_blocked");
      return;
    }
    markGoogleOAuthPopup(authTab);
    authTab.opener = null;
    popupRef.current = null;
    setAttempt("idle");
    setIsStarting(true);
    try {
      const response = await google.startAuthorize({
        redirect_uri: buildGoogleOAuthRedirectUri(),
        app_origin: getGoogleOAuthAppOrigin(),
        ...(card.reconnectConnectionId
          ? { credential_id: card.reconnectConnectionId }
          : {}),
        scope_profile: googleScopeProfile(card.scopeProfile),
        ...(setAccount ? { set_account: true } : {}),
      });
      storeGoogleOAuthIntegrationIdForState(response.state, card.integrationId);
      storeGoogleOAuthIntegrationIdForState(
        response.state,
        card.integrationId,
        authTab,
      );
      authTab.location.assign(response.authorize_url);
      popupRef.current = authTab;
      setAttempt("open");
    } catch (startError) {
      authTab.close();
      setAttempt(
        responseStatus(startError) === 403 ? "forbidden" : "start_failed",
      );
    } finally {
      setIsStarting(false);
    }
  };

  // Read back from the credential rows rather than this card's attempt, so the outcome survives a reload.
  const connected = reconnectConnectionId
    ? choices.find((choice) => choice.connection_id === reconnectConnectionId)
    : (choices.find(
        (choice) => choice.connection_id === selectedConnectionId,
      ) ??
      choices.find(
        (choice) =>
          initialIds !== null && !initialIds.has(choice.connection_id),
      ));
  const failure =
    attempt === "not_completed"
      ? `The connection was not completed. If your ${isGoogle ? "Google Workspace" : "Microsoft 365"} administrator blocks this app, ask them to allow it, then try again.`
      : ATTEMPT_FAILURES[attempt];
  const actionLabel = card.reconnectConnectionId ? "Reconnect" : "Connect";

  if (connected) {
    return (
      <div className="space-y-2">
        <div className="rounded-lg border border-success/40 bg-success/10 p-3 text-sm">
          <div className="font-medium">Connected</div>
          <div className="mt-1 text-xs text-muted-foreground">
            {connected.name}
            {connected.email_address ? ` (${connected.email_address})` : ""} can
            use {card.integrationName}.
          </div>
        </div>
        <ConnectedAccountChoiceCard
          choices={choices}
          accountKind={accountKind}
          selectedConnectionId={selectedConnectionId}
          disabled={disabled || !onSelect}
          onSelect={(connectionId) => onSelect?.(connectionId)}
        />
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
        <div className="font-medium">
          {actionLabel} {card.integrationName}
        </div>
        <div className="mt-1 text-xs text-muted-foreground">
          {choices.length > 0
            ? `Choose an account below, or connect another one for ${card.integrationName}.`
            : card.reconnectConnectionId
              ? `Reconnect this account so it can use ${card.integrationName}.`
              : `Connect an account to use ${card.integrationName}.`}
        </div>
        {failure ? (
          <div role="alert" className="mt-2 text-xs text-destructive">
            {failure}
          </div>
        ) : null}
        {isGoogle && attempt === "not_completed" ? (
          <button
            type="button"
            className="mt-2 text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground disabled:opacity-60"
            disabled={isStarting || !canAuthorize}
            onClick={() => void startGoogle()}
          >
            Try again
          </button>
        ) : isGoogle ? (
          <button
            type="button"
            className="mt-3 rounded bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-60"
            disabled={
              isStarting ||
              !canAuthorize ||
              (Boolean(card.reconnectConnectionId) && google.isLoading)
            }
            title={
              canAuthorize
                ? undefined
                : "Organization admin access is required."
            }
            onClick={() => void startGoogle()}
          >
            {isStarting
              ? "Opening…"
              : !canAuthorize
                ? "Admin required"
                : attempt === "open"
                  ? "Waiting for sign-in…"
                  : failure
                    ? `${actionLabel} again`
                    : actionLabel}
          </button>
        ) : (
          <a
            className="mt-3 inline-block rounded bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground"
            href="/integrations"
            target="_blank"
            rel="noreferrer"
          >
            {canAuthorize ? actionLabel : "Admin required"} on the Integrations
            page
          </a>
        )}
      </div>
      <ConfirmDialog
        open={confirmSetAccount}
        onOpenChange={setConfirmSetAccount}
        title="Set Google account?"
        description="This connection has no recorded Google account. Sign in with the account it should use. Workflows using this connection will use that account."
        reversible
        confirmLabel="Continue"
        confirmVariant="default"
        onConfirm={() => {
          setConfirmSetAccount(false);
          void startGoogle(true);
        }}
      />
      {choices.length > 0 ? (
        <ConnectedAccountChoiceCard
          choices={choices}
          accountKind={accountKind}
          selectedConnectionId={selectedConnectionId}
          disabled={disabled || !onSelect}
          onSelect={(connectionId) => onSelect?.(connectionId)}
        />
      ) : null}
    </div>
  );
}
