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
  useGoogleOAuthCredentials,
} from "@/hooks/useGoogleOAuthCredentials";
import {
  isMicrosoftOAuthCredentialActive,
  useMicrosoftOAuthCredentials,
} from "@/hooks/useMicrosoftOAuthCredentials";
import {
  type AccountFit,
  type IntegrationCatalogEntryWithActions,
} from "./types";

export type PickerAccount = {
  id: string;
  label: string;
  active: boolean;
  fit: AccountFit;
};

const CONNECT_OPTION = "__connect__";

// Fit comes from the server's account_fit; the editor never recomputes it. Accounts without a server
// fit (Microsoft today) are "unknown": enabled and flagged.
function useOAuthAccounts(
  provider: IntegrationCatalogEntryWithActions["oauthProvider"],
  actionKey: string,
): { accounts: PickerAccount[]; isLoading: boolean; isError: boolean } {
  const google = useGoogleOAuthCredentials({ enabled: provider === "google" });
  const microsoft = useMicrosoftOAuthCredentials({
    enabled: provider === "microsoft",
  });
  if (provider === "google") {
    return {
      isLoading: google.isLoading,
      isError: Boolean(google.error),
      accounts: google.credentials.map((credential) => ({
        id: credential.id,
        label: credential.credential_name,
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
      label: credential.credential_name,
      active: isMicrosoftOAuthCredentialActive(credential),
      fit: "unknown",
    })),
  };
}

function accountHint(account: PickerAccount): string | null {
  if (!account.active) return "Needs reconnect";
  if (account.fit === "no")
    return "Missing permission for this action. Reconnect it on Integrations.";
  if (account.fit === "unknown") return "Permissions unknown";
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

// Native accounts only: the integration action block cannot run on a Nango connection id yet.
function OAuthAccountPicker({
  entry,
  actionKey,
  value,
  onChange,
  disabled,
}: Props) {
  const { accounts, isLoading, isError } = useOAuthAccounts(
    entry.oauthProvider,
    actionKey,
  );
  if (isLoading) return <Skeleton className="h-9 w-full" />;
  const saved = accounts.find((account) => account.id === value);
  const savedHint = saved ? accountHint(saved) : null;
  return (
    <div className="space-y-2">
      {isError ? (
        <p className="text-xs text-amber-600 dark:text-amber-300">
          Could not load {entry.name} accounts.
        </p>
      ) : value && !saved ? (
        <p className="text-xs text-amber-600 dark:text-amber-300">
          Saved account is no longer connected. Pick another below.
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
                  {hint ? (
                    <p className="text-xs text-muted-foreground">{hint}</p>
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

// Nango-backed apps have no account list in the editor yet, so they take a raw connection id.
function IntegrationAccountPicker(props: Props) {
  if (props.entry.oauthProvider) return <OAuthAccountPicker {...props} />;
  return <ConnectionIdField {...props} />;
}

export { IntegrationAccountPicker };
