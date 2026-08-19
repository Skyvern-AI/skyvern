import { useState } from "react";

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
import { toast } from "@/components/ui/use-toast";
import { GOOGLE_SHEETS_BLOCK_REQUIRED_SCOPES } from "@/util/googleScopes";

import type { GoogleConnectionNotice } from "../narrativeState";

export function GoogleReconnectCard({
  notice,
}: {
  notice: GoogleConnectionNotice;
}) {
  const { credentials, isLoading, isFetching, error, startAuthorize } =
    useGoogleOAuthCredentials({ refetchOnMount: "always" });
  const [isStarting, setIsStarting] = useState(false);
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
    hasGoogleOAuthCredentialScopes(
      exactCredential,
      GOOGLE_SHEETS_BLOCK_REQUIRED_SCOPES,
    )
  ) {
    return (
      <div
        role="status"
        className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 p-3 text-sm"
      >
        <div className="font-medium">{accountName} reconnected</div>
        <div className="mt-1 text-xs text-muted-foreground">
          This Google Sheets connection is ready to use.
        </div>
      </div>
    );
  }

  if (notice.condition === "missing") {
    return (
      <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
        <div className="font-medium">
          Google Sheets connection is no longer available
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
    const authTab = window.open(
      "",
      "skyvern-google-oauth",
      "popup,width=600,height=760",
    );
    if (!authTab) {
      toast({
        title: "Unable to open Google",
        description: "Allow pop-ups for this site, then try Reconnect again.",
        variant: "destructive",
      });
      return;
    }
    markGoogleOAuthPopup(authTab);
    authTab.opener = null;
    setIsStarting(true);
    try {
      const response = await startAuthorize({
        redirect_uri: buildGoogleOAuthRedirectUri(),
        app_origin: getGoogleOAuthAppOrigin(),
        credential_id: notice.connectionId,
        scope_profile: "google_sheets",
      });
      storeGoogleOAuthIntegrationIdForState(response.state, "google_sheets");
      storeGoogleOAuthIntegrationIdForState(
        response.state,
        "google_sheets",
        authTab,
      );
      authTab.location.assign(response.authorize_url);
    } catch {
      authTab.close();
    } finally {
      setIsStarting(false);
    }
  };

  return (
    <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
      <div className="font-medium">{accountName} needs to be reconnected</div>
      <div className="mt-1 text-xs text-muted-foreground">
        The workflow draft was saved, but this Sheets connection is not usable.
      </div>
      <button
        type="button"
        className="mt-3 rounded bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-60"
        disabled={isStarting}
        onClick={() => void reconnect()}
      >
        {isStarting ? "Opening Google…" : "Reconnect"}
      </button>
    </div>
  );
}
