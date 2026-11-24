import { useEffect, useRef, useState } from "react";
import { usePostHog } from "posthog-js/react";

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
import { useCurrentOrgId } from "@/hooks/useCurrentOrgId";
import { stripBidiIsolates } from "@/util/bidiText";
import {
  type ConnectedGmailFlow,
  useHasCapability,
  usePageSlots,
} from "@/store/PageSlots";
import { parseServerStamp } from "@/util/utils";

import {
  connectionCardKey,
  type ConnectionCard as ConnectionCardData,
} from "../narrativeState";
import type { ConnectedAccountChoice } from "../emberTypes";
import { ConnectedAccountChoiceCard } from "./ConnectedAccountChoiceCard";
import {
  type GoogleConnectionAttempt,
  googleConnectionOutcome,
} from "./googleConnectionOutcome";

const GOOGLE_SCOPE_PROFILES: readonly GoogleOAuthScopeProfile[] = [
  "google_sheets",
  "gmail",
  "google_drive",
  "gmail_send",
  "gmail_read_send",
];

const POPUP_POLL_MS = 500;

const ATTEMPT_STATES = [
  "idle",
  "open",
  "not_completed",
  "partial",
  "popup_blocked",
  "forbidden",
  "start_failed",
] as const;

type AttemptState = (typeof ATTEMPT_STATES)[number];

// The partial row stays the reconnect target until a later attempt settles, so it rides along with every state.
type Outcome = { attempt: AttemptState; partialConnectionId?: string };

const IDLE: Outcome = { attempt: "idle" };

const OUTCOME_STORAGE_PREFIX = "skyvern-ember-connection-outcome:";

// A reload loses the popup, so an open attempt comes back idle; settled outcomes come back as they were.
function readOutcome(storageKey: string | null): Outcome {
  if (!storageKey) return IDLE;
  try {
    const stored = JSON.parse(sessionStorage.getItem(storageKey) ?? "null");
    const attempt = ATTEMPT_STATES.find((state) => state === stored?.attempt);
    if (!attempt) return IDLE;
    return {
      attempt: attempt === "open" ? "idle" : attempt,
      ...(typeof stored.partialConnectionId === "string"
        ? { partialConnectionId: stored.partialConnectionId }
        : {}),
    };
  } catch {
    return IDLE;
  }
}

function writeOutcome(storageKey: string | null, outcome: Outcome) {
  if (!storageKey) return;
  try {
    if (outcome.attempt === "idle" && !outcome.partialConnectionId) {
      sessionStorage.removeItem(storageKey);
    } else {
      sessionStorage.setItem(storageKey, JSON.stringify(outcome));
    }
  } catch {
    // Storage can be unavailable; the outcome then lasts until a reload.
  }
}

const ATTEMPT_FAILURES: Partial<Record<AttemptState, string>> = {
  not_completed: "The connection was not completed.",
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

type ConnectionCardProps = {
  card: ConnectionCardData;
  turnId?: string | null;
  // Server time the card's turn was posted. An account created after it shows as Connected, whoever made it, but is
  // sent as the pick only when it is this card's own attempt.
  postedAt?: string | null;
  selectedConnectionId?: string | null;
  disabled?: boolean;
  onSelect?: (connectionId: string) => void;
  // The account the card shows as picked, so the pick's receipt can name it.
  onSelectionShown?: (choice: ConnectedAccountChoice) => void;
};

export function ConnectionCard(props: ConnectionCardProps) {
  const { connectedGmailFlow: GmailFlow } = usePageSlots();
  // A reconnect keeps its native credential; only a new Gmail connection follows the Connected Gmail default.
  if (
    !GmailFlow ||
    props.card.integrationId !== "gmail" ||
    props.card.reconnectConnectionId
  ) {
    return <ConnectionCardBody {...props} />;
  }
  return (
    <GmailFlow>
      {(flow) => <ConnectionCardBody {...props} connectedGmail={flow} />}
    </GmailFlow>
  );
}

function ConnectionCardBody({
  card,
  turnId = null,
  postedAt = null,
  selectedConnectionId = null,
  disabled = false,
  onSelect,
  onSelectionShown,
  connectedGmail,
}: ConnectionCardProps & { connectedGmail?: ConnectedGmailFlow }) {
  // Once the server answers native sign-in with 409, Connected Gmail is the card's flow while it can connect, so its
  // failure and retry show; a flow that can no longer connect falls back to native sign-in like any non-live flow.
  const [switchedToConnectedGmail, setSwitchedToConnectedGmail] =
    useState(false);
  // An existing Connected Gmail account stays a choice even when a native credential makes it no longer the default.
  // A step that runs only on Connected Gmail always takes that flow, even one that cannot connect: native sign-in would
  // add an account the step cannot use.
  const gmailFlow =
    connectedGmail?.status === "default" ||
    (switchedToConnectedGmail && connectedGmail?.canConnect) ||
    card.connectedGmailOnly
      ? connectedGmail
      : undefined;
  const gmailFlowLoading = connectedGmail?.status === "loading";
  // A Connected Gmail sign-in can run after a 409 switch even when native is the card's default, so its progress shows either way.
  const postHog = usePostHog();
  const orgId = useCurrentOrgId();
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
  // While the flow decides, the button stays a disabled Connect rather than guessing at an admin note.
  const canAuthorize = gmailFlowLoading
    ? true
    : gmailFlow
      ? gmailFlow.canConnect
      : card.connectedGmailOnly
        ? false
        : isGoogle
          ? canGoogleAuthorize
          : canMicrosoftAuthorize;
  // An admin whose Connected Gmail flow cannot connect is blocked by the flow, not by their role.
  const connectedGmailUnavailable =
    Boolean(card.connectedGmailOnly) && !canAuthorize && canGoogleAuthorize;
  const [isStarting, setIsStarting] = useState(false);
  const [confirmSetAccount, setConfirmSetAccount] = useState(false);
  const outcomeKey = turnId
    ? `${OUTCOME_STORAGE_PREFIX}${turnId}:${connectionCardKey(card)}`
    : null;
  const [outcome, setOutcomeState] = useState<Outcome>(() =>
    readOutcome(outcomeKey),
  );
  const settle = (next: Outcome) => {
    setOutcomeState(next);
    writeOutcome(outcomeKey, next);
  };
  const settleRef = useRef(settle);
  settleRef.current = settle;
  // An account this card's own sign-in brought in is the user's pick, sent as one at once so the chat stores it.
  // A pick that arrives while the card is disabled is dropped and the picker asks instead; an account is sent at
  // most once per card.
  const pickedIdRef = useRef<string | null>(null);
  const pickRef = useRef<(connectionId: string) => void>(() => {});
  pickRef.current = (connectionId: string) => {
    if (disabled || !onSelect || pickedIdRef.current === connectionId) return;
    pickedIdRef.current = connectionId;
    onSelect(connectionId);
  };
  const { attempt, partialConnectionId } = outcome;
  const popupRef = useRef<Window | null>(null);
  const snapshotRef = useRef<GoogleOAuthCredential[]>([]);
  const attemptRef = useRef<GoogleConnectionAttempt | null>(null);
  const refetchGoogle = google.refetchCredentials;
  const targetConnectionId =
    card.reconnectConnectionId ?? partialConnectionId ?? null;
  const requiredScopesKey = card.requiredScopes.join(" ");
  const postedMs = postedAt ? parseServerStamp(postedAt) : Number.NaN;
  // Connected Gmail rows carry no creation time, so they keep a baseline taken once the flow has loaded; one that
  // already existed then never reads as just connected.
  const [initialGmailIds, setInitialGmailIds] = useState<Set<string> | null>(
    null,
  );
  const gmailIdsKey = (connectedGmail?.connections ?? [])
    .map((connection) => connection.connection_id)
    .join(" ");
  useEffect(() => {
    if (initialGmailIds === null && !gmailFlowLoading) {
      setInitialGmailIds(new Set(gmailIdsKey.split(" ").filter(Boolean)));
    }
  }, [initialGmailIds, gmailFlowLoading, gmailIdsKey]);
  // Only the flow's own finished sign-in counts, never an account that appears in the list some other way.
  const completedGmailId = connectedGmail?.completedConnectionId ?? null;
  useEffect(() => {
    if (!completedGmailId) return;
    // Out of the effect, since the send flushes editor state synchronously.
    queueMicrotask(() => pickRef.current(completedGmailId));
  }, [completedGmailId]);
  const newIds = new Set([
    ...(isGoogle ? google.credentials : microsoft.credentials)
      .filter(
        (credential) => parseServerStamp(credential.created_at) > postedMs,
      )
      .map((credential) => credential.id),
    ...gmailIdsKey
      .split(" ")
      .filter(
        (id) => id && initialGmailIds !== null && !initialGmailIds.has(id),
      ),
  ]);

  useEffect(() => {
    if (attempt !== "open") return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      if (!popupRef.current?.closed) return;
      window.clearInterval(timer);
      popupRef.current = null;
      void refetchGoogle()
        .then(({ data }) =>
          googleConnectionOutcome(
            snapshotRef.current,
            data ?? [],
            attemptRef.current,
            requiredScopesKey.split(" ").filter(Boolean),
          ),
        )
        .catch(() => ({ kind: "not_completed" }) as const)
        .then((result) => {
          if (cancelled) return;
          if (result.kind === "connected") pickRef.current(result.connectionId);
          settleRef.current(
            result.kind === "connected" || result.kind === "unattributed"
              ? IDLE
              : result.kind === "partial"
                ? {
                    attempt: "partial",
                    partialConnectionId: result.connectionId,
                  }
                : { attempt: "not_completed", partialConnectionId },
          );
        });
    }, POPUP_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [attempt, refetchGoogle, partialConnectionId, requiredScopesKey]);

  const nativeChoices: ConnectedAccountChoice[] = isGoogle
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
  const choices: ConnectedAccountChoice[] = [
    ...(card.connectedGmailOnly ? [] : nativeChoices),
    ...(connectedGmail?.connections ?? []).map((connection) => ({
      connection_id: connection.connection_id,
      name: connection.name,
      state: "active" as const,
      email_address: connection.email_address ?? null,
    })),
  ];

  // The same connect events the Integrations page sends; the card has no naming step, so a start is also the submit.
  const captureConnectStarted = () => {
    postHog?.capture("integrations.connect_clicked", {
      integration_id: card.integrationId,
      integration_name: card.integrationName,
      surface: "copilot",
    });
    postHog?.capture(`integrations.${card.provider}.connect.started`, {
      org_id: orgId,
      surface: "copilot",
    });
    postHog?.capture("integrations.connect_submitted", {
      integration_id: card.integrationId,
      surface: "copilot",
    });
  };

  const startConnectedGmail = () => {
    if (!gmailFlow) return;
    captureConnectStarted();
    gmailFlow.start();
  };

  const targetCredential = google.credentials.find(
    (credential) => credential.id === targetConnectionId,
  );

  const startGoogle = async (setAccount = false) => {
    if (!setAccount && targetCredential?.account_recorded === false) {
      setConfirmSetAccount(true);
      return;
    }
    captureConnectStarted();
    snapshotRef.current = google.credentials;
    attemptRef.current = null;
    // One named window per card, so starting a sibling card's consent cannot take over this card's popup.
    const authTab = window.open(
      "",
      `skyvern-oauth-${connectionCardKey(card)}`,
      "popup,width=600,height=760",
    );
    if (!authTab) {
      settle({ attempt: "popup_blocked", partialConnectionId });
      return;
    }
    markGoogleOAuthPopup(authTab);
    authTab.opener = null;
    popupRef.current = null;
    settle({ attempt: "idle", partialConnectionId });
    setIsStarting(true);
    try {
      const response = await google.startAuthorize({
        redirect_uri: buildGoogleOAuthRedirectUri(),
        app_origin: getGoogleOAuthAppOrigin(),
        ...(targetConnectionId ? { credential_id: targetConnectionId } : {}),
        scope_profile: googleScopeProfile(card.scopeProfile),
        ...(setAccount ? { set_account: true } : {}),
      });
      attemptRef.current = response.credential_id
        ? {
            credentialId: response.credential_id,
            reconnect: Boolean(targetConnectionId),
          }
        : null;
      storeGoogleOAuthIntegrationIdForState(response.state, card.integrationId);
      storeGoogleOAuthIntegrationIdForState(
        response.state,
        card.integrationId,
        authTab,
      );
      authTab.location.assign(response.authorize_url);
      popupRef.current = authTab;
      settle({ attempt: "open", partialConnectionId });
    } catch (startError) {
      authTab.close();
      // The server found Connected Gmail is the way for this org after this card fell back to native (409).
      if (responseStatus(startError) === 409 && connectedGmail?.canConnect) {
        settle({ attempt: "idle", partialConnectionId });
        setSwitchedToConnectedGmail(true);
        connectedGmail.start();
        return;
      }
      // The partly granted account was deleted or can no longer be reauthorized, so the card offers a fresh connect.
      if (
        responseStatus(startError) === 404 &&
        partialConnectionId &&
        !card.reconnectConnectionId
      ) {
        settle(IDLE);
        return;
      }
      settle({
        attempt:
          responseStatus(startError) === 403 ? "forbidden" : "start_failed",
        partialConnectionId,
      });
    } finally {
      setIsStarting(false);
    }
  };

  // Read back from the credential rows rather than this card's attempt, so the outcome survives a reload.
  const connected = card.reconnectConnectionId
    ? choices.find(
        (choice) => choice.connection_id === card.reconnectConnectionId,
      )
    : (choices.find(
        (choice) => choice.connection_id === selectedConnectionId,
      ) ??
      choices.find((choice) => newIds.has(choice.connection_id)) ??
      // A user's one Connected Gmail account is already the way to use Gmail, so the card settles on it.
      (gmailFlow && !gmailFlow.canConnect
        ? choices.find((choice) =>
            gmailFlow.connections.some(
              (connection) => connection.connection_id === choice.connection_id,
            ),
          )
        : undefined));
  // Google shows an administrator block on its own page and never returns to the callback, so a popup closed
  // with nothing changed carries no cause and the card does not guess one.
  const failure =
    gmailFlow?.failure ||
    (attempt === "partial"
      ? `${targetCredential?.credential_name ?? "The account"} was connected without the ${card.integrationName} permission. Reconnect it and leave every permission checked.`
      : ATTEMPT_FAILURES[attempt]);
  const actionLabel = targetConnectionId ? "Reconnect" : "Connect";
  // The account a new Gmail card cannot add to: the user's one Connected Gmail, there before this card.
  const onlyConnectedGmail =
    !card.reconnectConnectionId &&
    connected &&
    gmailFlow &&
    !gmailFlow.canConnect &&
    !newIds.has(connected.connection_id) &&
    gmailFlow.connections.some(
      (connection) => connection.connection_id === connected.connection_id,
    );
  const shown = choices.find(
    (choice) => choice.connection_id === selectedConnectionId,
  );
  const shownId = shown?.connection_id;
  const shownName = shown?.name;
  const shownEmail = shown?.email_address ?? null;
  useEffect(() => {
    if (shownId && shownName !== undefined) {
      onSelectionShown?.({
        connection_id: shownId,
        name: shownName,
        state: "active",
        email_address: shownEmail,
      });
    }
  }, [shownId, shownName, shownEmail, onSelectionShown]);

  if (connected) {
    return (
      <div className="space-y-2">
        <div className="rounded-lg border border-success/40 bg-success/10 p-3 text-sm">
          {onlyConnectedGmail ? (
            <>
              <div className="font-medium">Connected</div>
              <div className="mt-1 text-xs text-muted-foreground">
                {connected.connection_id === selectedConnectionId
                  ? "Using your Connected Gmail account "
                  : "Your Connected Gmail account "}
                <bdi>
                  {connected.email_address ?? stripBidiIsolates(connected.name)}
                </bdi>
                {connected.connection_id === selectedConnectionId
                  ? ". "
                  : " is shown below. Pick it to use it here. "}
                To use a different Gmail account, reconnect or disconnect it on
                the Integrations page.
              </div>
              <a
                className="mt-2 inline-block text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground"
                href="/integrations"
                target="_blank"
                rel="noreferrer"
              >
                Open the Integrations page
              </a>
            </>
          ) : (
            <>
              <div className="font-medium">Connected</div>
              <div className="mt-1 text-xs text-muted-foreground">
                <bdi>{stripBidiIsolates(connected.name)}</bdi>
                {connected.email_address
                  ? ` (${connected.email_address})`
                  : ""}{" "}
                can use {card.integrationName}.
              </div>
            </>
          )}
        </div>
        {choices.length === 1 &&
        choices[0]?.connection_id === selectedConnectionId ? null : (
          <ConnectedAccountChoiceCard
            choices={choices}
            accountKind={accountKind}
            selectedConnectionId={selectedConnectionId}
            disabled={disabled || !onSelect}
            onSelect={(connectionId) => onSelect?.(connectionId)}
          />
        )}
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
            : targetConnectionId
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
              Boolean(connectedGmail?.busy) ||
              gmailFlowLoading ||
              !canAuthorize ||
              (Boolean(targetConnectionId) && google.isLoading)
            }
            title={
              canAuthorize
                ? undefined
                : connectedGmailUnavailable
                  ? "Connected Gmail is not available right now. Reload the page and try again."
                  : "Organization admin access is required."
            }
            onClick={() =>
              gmailFlow ? startConnectedGmail() : void startGoogle()
            }
          >
            {isStarting
              ? "Opening…"
              : !canAuthorize
                ? connectedGmailUnavailable
                  ? "Unavailable"
                  : "Admin required"
                : attempt === "open" || connectedGmail?.busy
                  ? "Waiting for sign-in…"
                  : failure && attempt !== "partial"
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
