import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import {
  CustomSelectItem,
  Select,
  SelectContent,
  SelectItem,
  SelectItemText,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  isGoogleOAuthCredentialActive,
  oauthAccountLabel,
  useGoogleOAuthCredentials,
} from "@/hooks/useGoogleOAuthCredentials";
import {
  isMicrosoftOAuthCredentialActive,
  useMicrosoftOAuthCredentials,
} from "@/hooks/useMicrosoftOAuthCredentials";
import { type ConnectedGmailFlow, usePageSlots } from "@/store/PageSlots";
import { useEffect } from "react";
import { rememberAccounts } from "./knownAccounts";
import {
  type AccountFit,
  type IntegrationCatalogEntryWithActions,
} from "./types";

export type PickerAccount = {
  id: string;
  label: string;
  active: boolean;
  fit: AccountFit;
  providerHint?: string;
};

const CONNECT_OPTION = "__connect__";

// Fit comes from the server's account_fit; the editor never recomputes it. Accounts without a server
// fit (Microsoft today) are "unknown": enabled and flagged.
function useOAuthAccounts(
  provider: IntegrationCatalogEntryWithActions["oauthProvider"],
  actionKey: string,
): { accounts: PickerAccount[]; isLoading: boolean; isError: boolean } {
  const google = useGoogleOAuthCredentials({
    enabled: provider === "google",
    includeEmail: true,
  });
  const microsoft = useMicrosoftOAuthCredentials({
    enabled: provider === "microsoft",
    includeEmail: true,
  });
  if (provider === "google") {
    return {
      isLoading: google.isLoading,
      isError: Boolean(google.error),
      accounts: google.credentials.map((credential) => ({
        id: credential.id,
        label: oauthAccountLabel(credential),
        active: isGoogleOAuthCredentialActive(credential),
        fit: credential.action_fit?.[actionKey] ?? "unknown",
      })),
    };
  }
  return {
    isLoading: microsoft.isLoading,
    isError: Boolean(microsoft.error),
    accounts: microsoft.credentials.map((credential) => ({
      id: credential.id,
      label: oauthAccountLabel(credential),
      active: isMicrosoftOAuthCredentialActive(credential),
      fit: "unknown",
    })),
  };
}

function accountHint(account: PickerAccount): string | null {
  if (!account.active) return "Needs reconnect";
  if (account.fit === "no")
    return "Missing permission for this action. Reconnect it on Integrations.";
  if (account.fit === "unknown" && !account.providerHint)
    return "Permissions unknown";
  return null;
}

type Props = {
  nodeId: string;
  entry: IntegrationCatalogEntryWithActions;
  // "<integration id>.<action id>", the key of the server's per-account action fit.
  actionKey: string;
  value: string;
  onChange: (connectionId: string) => void;
  disabled?: boolean;
};

function ConnectionIdField({
  nodeId,
  entry,
  value,
  onChange,
  disabled,
}: Props) {
  return (
    <div className="space-y-1">
      <WorkflowBlockInputTextarea
        nodeId={nodeId}
        name="connectionId"
        value={value}
        onChange={onChange}
        placeholder="Connection ID"
        className="nopan text-xs"
        disabled={disabled}
        aria-label={`${entry.name} account`}
      />
      <p className="text-xs text-muted-foreground">Permissions unknown</p>
    </div>
  );
}

function OAuthAccountPicker({
  entry,
  actionKey,
  value,
  onChange,
  disabled,
  additionalAccounts = [],
  additionalLoading = false,
  additionalError = false,
  brokerAccountIds = [],
  missingSavedHint,
}: Props & {
  additionalAccounts?: PickerAccount[];
  additionalLoading?: boolean;
  additionalError?: boolean;
  // Every loaded broker account, including ones this action cannot use and so does not list.
  brokerAccountIds?: readonly string[];
  missingSavedHint?: string;
}) {
  const oauth = useOAuthAccounts(entry.oauthProvider, actionKey);
  const accounts = [...oauth.accounts, ...additionalAccounts];
  useEffect(() => {
    rememberAccounts(
      oauth.accounts.map((account) => account.id),
      "native",
    );
    rememberAccounts(brokerAccountIds, "nango");
  });
  if (oauth.isLoading || additionalLoading)
    return <Skeleton className="h-9 w-full" />;
  const saved = accounts.find((account) => account.id === value);
  const savedHint = saved ? accountHint(saved) : null;
  return (
    <div className="ph-no-capture space-y-2" data-private>
      {oauth.isError || additionalError ? (
        <p className="text-xs text-amber-600 dark:text-amber-300">
          Could not load {entry.name} accounts.
        </p>
      ) : value && !saved ? (
        <p className="text-xs text-amber-600 dark:text-amber-300">
          {missingSavedHint ??
            "Saved account is no longer connected. Pick another below."}
        </p>
      ) : savedHint ? (
        <p className="text-xs text-amber-600 dark:text-amber-300">
          {savedHint}
        </p>
      ) : null}
      <Select
        value={value}
        disabled={disabled}
        onValueChange={(selected) => {
          if (selected === CONNECT_OPTION) {
            window.open("/integrations", "_blank", "noopener,noreferrer");
            return;
          }
          onChange(selected);
        }}
      >
        <SelectTrigger
          className="nopan text-xs"
          aria-label={`${entry.name} account`}
        >
          <SelectValue placeholder={`Select a ${entry.name} account`} />
        </SelectTrigger>
        <SelectContent>
          {accounts.map((account) => {
            const hint = accountHint(account);
            const detail = hint ?? account.providerHint;
            return (
              <CustomSelectItem
                key={account.id}
                value={account.id}
                disabled={!account.active || account.fit === "no"}
              >
                <div className="space-y-0.5">
                  <p className="text-sm font-medium">
                    <SelectItemText>{account.label}</SelectItemText>
                  </p>
                  {detail ? (
                    <p className="text-xs text-muted-foreground">{detail}</p>
                  ) : null}
                </div>
              </CustomSelectItem>
            );
          })}
          <SelectItem value={CONNECT_OPTION}>
            Connect another account
          </SelectItem>
        </SelectContent>
      </Select>
    </div>
  );
}

function connectedDriveAction(
  entry: IntegrationCatalogEntryWithActions,
  actionKey: string,
) {
  if (entry.id !== "google_drive") return;
  const prefix = `${entry.id}.`;
  if (!actionKey.startsWith(prefix)) return;
  return entry.actions?.find(
    (item) => item.id === actionKey.slice(prefix.length),
  );
}

function connectedDriveAccounts(flow: ConnectedGmailFlow): PickerAccount[] {
  return flow.connections.map((connection) => ({
    id: connection.connection_id,
    label: connection.email_address
      ? `${connection.name} (${connection.email_address})`
      : connection.name,
    active: true,
    fit: "unknown",
    providerHint: "Connected account",
  }));
}

function IntegrationAccountPicker(props: Props) {
  const { connectedGoogleDriveFlow: ConnectedDriveFlow } = usePageSlots();
  if (props.entry.oauthProvider) {
    const driveAction = connectedDriveAction(props.entry, props.actionKey);
    if (ConnectedDriveFlow && driveAction) {
      return (
        <ConnectedDriveFlow>
          {(flow) => {
            const supportsConnected =
              driveAction.credentialBackends?.includes("nango") === true;
            const brokerAccountIds = flow.connections.map(
              (connection) => connection.connection_id,
            );
            return (
              <OAuthAccountPicker
                {...props}
                additionalAccounts={
                  supportsConnected ? connectedDriveAccounts(flow) : []
                }
                additionalLoading={
                  supportsConnected && flow.status === "loading"
                }
                additionalError={supportsConnected && Boolean(flow.failure)}
                brokerAccountIds={brokerAccountIds}
                missingSavedHint={
                  !supportsConnected && brokerAccountIds.includes(props.value)
                    ? `Connected Drive accounts do not support ${driveAction.name}. Pick a native account below.`
                    : undefined
                }
              />
            );
          }}
        </ConnectedDriveFlow>
      );
    }
    return <OAuthAccountPicker {...props} />;
  }
  return <ConnectionIdField {...props} />;
}

export { IntegrationAccountPicker };
