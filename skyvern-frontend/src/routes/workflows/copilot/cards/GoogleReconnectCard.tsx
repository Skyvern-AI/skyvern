import { useEffect, useRef, useState, type ReactNode } from "react";

import {
  hasGoogleOAuthCredentialScopes,
  isGoogleOAuthCredentialActive,
  useGoogleOAuthCredentials,
} from "@/hooks/useGoogleOAuthCredentials";
import {
  buildGoogleOAuthRedirectUri,
  getGoogleOAuthAppOrigin,
  storeGoogleOAuthIntegrationIdForState,
} from "@/routes/integrations/googleOAuth";
import { markGoogleOAuthPopup } from "@/routes/integrations/googleOAuthPopup";
import { useHasCapability } from "@/store/PageSlots";
import {
  GOOGLE_GMAIL_REQUIRED_SCOPES,
  GOOGLE_SHEETS_BLOCK_REQUIRED_SCOPES,
} from "@/util/googleScopes";

import {
  googleConnectionNoticeKey,
  type GoogleConnectionNotice,
  type GoogleSetupScopeProfile,
} from "../narrativeState";
import { ConnectedAccountChoiceCard } from "./ConnectedAccountChoiceCard";

const PROFILE_SCOPES: Record<GoogleSetupScopeProfile, readonly string[]> = {
  gmail: GOOGLE_GMAIL_REQUIRED_SCOPES,
  google_sheets: GOOGLE_SHEETS_BLOCK_REQUIRED_SCOPES,
};

const PROFILE_LABELS: Record<GoogleSetupScopeProfile, string> = {
  gmail: "Gmail (read only)",
  google_sheets: "Google Sheets",
};

const POPUP_POLL_MS = 500;

type AttemptState =
  | "idle"
  | "open"
  | "not_completed"
  | "popup_blocked"
  | "forbidden"
  | "start_failed";

const ATTEMPT_FAILURES: Partial<Record<AttemptState, string>> = {
  not_completed: "Google connection was not completed.",
  popup_blocked:
    "Your browser blocked the Google window. Allow pop-ups for this site, then try again.",
  forbidden: "Organization admin access is required to connect Google.",
  start_failed: "Could not start the Google connection.",
};

function responseStatus(error: unknown): number | undefined {
  return (error as { response?: { status?: number } } | null)?.response?.status;
}

export function GoogleReconnectCard({
  notice,
  selectedConnectionId = null,
  disabled = false,
  onSelect,
}: {
  notice: GoogleConnectionNotice;
  selectedConnectionId?: string | null;
  disabled?: boolean;
  onSelect?: (connectionId: string) => void;
}) {
  const {
    credentials,
    isLoading,
    isFetching,
    error,
    refetchCredentials,
    startAuthorize,
  } = useGoogleOAuthCredentials({
    refetchOnMount: notice.condition === "unbound" ? undefined : "always",
  });
  const canAuthorize = useHasCapability("google_oauth.authorize");
  const [isStarting, setIsStarting] = useState(false);
  const [attempt, setAttempt] = useState<AttemptState>("idle");
  const popupRef = useRef<Window | null>(null);
  const requiredScopes = PROFILE_SCOPES[notice.scopeProfile];
  const profileLabel = PROFILE_LABELS[notice.scopeProfile];
  const isSetup = notice.origin === "setup";
  const isUnbound = notice.condition === "unbound";

  useEffect(() => {
    if (attempt !== "open") return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      if (!popupRef.current?.closed) return;
      window.clearInterval(timer);
      popupRef.current = null;
      void refetchCredentials().finally(() => {
        if (!cancelled) setAttempt("not_completed");
      });
    }, POPUP_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [attempt, refetchCredentials]);

  const withProfileCaption = (card: ReactNode) =>
    isSetup ? (
      <div className="space-y-1">
        <div className="text-xs font-medium text-muted-foreground">
          Choose a Google account for {profileLabel}
        </div>
        {card}
      </div>
    ) : (
      card
    );

  const choices =
    isLoading || error
      ? (notice.choices ?? [])
      : credentials
          .filter(
            (credential) =>
              isGoogleOAuthCredentialActive(credential) &&
              hasGoogleOAuthCredentialScopes(credential, requiredScopes),
          )
          .map((credential) => ({
            connection_id: credential.id,
            name: credential.credential_name,
            state: "active",
            email_address: credential.email_address ?? null,
          }));
  if (isUnbound && choices.length > 0) {
    return withProfileCaption(
      <ConnectedAccountChoiceCard
        choices={choices}
        selectedConnectionId={selectedConnectionId}
        disabled={disabled || !onSelect}
        onSelect={(connectionId) => onSelect?.(connectionId)}
      />,
    );
  }
  const exactCredential = credentials.find(
    (credential) => credential.id === notice.connectionId,
  );
  const accountName = notice.displayName ?? "This Google account";
  if (
    !isLoading &&
    !isFetching &&
    !error &&
    exactCredential &&
    isGoogleOAuthCredentialActive(exactCredential) &&
    hasGoogleOAuthCredentialScopes(exactCredential, requiredScopes)
  ) {
    if (isSetup) {
      return withProfileCaption(
        <ConnectedAccountChoiceCard
          choices={[
            {
              connection_id: exactCredential.id,
              name: exactCredential.credential_name,
              state: "active",
              email_address: exactCredential.email_address ?? null,
            },
          ]}
          selectedConnectionId={selectedConnectionId}
          disabled={disabled || !onSelect}
          onSelect={(connectionId) => onSelect?.(connectionId)}
        />,
      );
    }
    return (
      <div
        role="status"
        className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 p-3 text-sm"
      >
        <div className="font-medium">{accountName} reconnected</div>
        <div className="mt-1 text-xs text-muted-foreground">
          This {profileLabel} connection is ready to use.
        </div>
      </div>
    );
  }

  if (notice.condition === "missing") {
    return (
      <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
        <div className="font-medium">
          {profileLabel} connection is no longer available
        </div>
        <div className="mt-1 text-xs text-muted-foreground">
          The workflow draft was saved, but this connection was removed and
          cannot be reconnected. Choose an available Google account in the
          Sheets block.
        </div>
      </div>
    );
  }

  const reconnect = async () => {
    // One named window per card, so starting a sibling card's consent cannot take over this card's popup.
    const authTab = window.open(
      "",
      `skyvern-google-oauth-${googleConnectionNoticeKey(notice)}`,
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
      const response = await startAuthorize({
        redirect_uri: buildGoogleOAuthRedirectUri(),
        app_origin: getGoogleOAuthAppOrigin(),
        ...(notice.connectionId ? { credential_id: notice.connectionId } : {}),
        scope_profile: notice.scopeProfile,
      });
      storeGoogleOAuthIntegrationIdForState(
        response.state,
        notice.scopeProfile,
      );
      storeGoogleOAuthIntegrationIdForState(
        response.state,
        notice.scopeProfile,
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

  const failure = ATTEMPT_FAILURES[attempt];
  const retrying = failure !== undefined;
  const connectLabel = isUnbound ? "Connect" : "Reconnect";

  return (
    <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
      <div className="font-medium">
        {isUnbound
          ? isSetup
            ? `Connect ${profileLabel}`
            : "Connect a Google account"
          : `${accountName} needs to be reconnected`}
      </div>
      <div className="mt-1 text-xs text-muted-foreground">
        {isSetup
          ? isUnbound
            ? `Connect a Google account to use ${profileLabel}.`
            : `This ${profileLabel} connection is not usable.`
          : isUnbound
            ? "The workflow draft was saved. Connect an account to use Google Sheets."
            : "The workflow draft was saved, but this Sheets connection is not usable."}
      </div>
      {failure ? (
        <div role="alert" className="mt-2 text-xs text-destructive">
          {failure}
        </div>
      ) : null}
      <button
        type="button"
        className="mt-3 rounded bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-60"
        disabled={isStarting || !canAuthorize}
        title={
          canAuthorize ? undefined : "Organization admin access is required."
        }
        onClick={() => void reconnect()}
      >
        {isStarting
          ? "Opening Google…"
          : !canAuthorize
            ? "Admin required"
            : attempt === "open"
              ? "Waiting for Google…"
              : retrying
                ? `${connectLabel} again`
                : connectLabel}
      </button>
    </div>
  );
}
