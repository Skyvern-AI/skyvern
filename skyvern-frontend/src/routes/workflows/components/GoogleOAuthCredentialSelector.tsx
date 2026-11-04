import { useState } from "react";
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
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { useHasCapability } from "@/store/PageSlots";
import {
  getDefaultGoogleOAuthCredentialId,
  hasGoogleOAuthCredentialScopes,
  isGoogleOAuthCredentialActive,
  useGoogleOAuthCredentials,
} from "@/hooks/useGoogleOAuthCredentials";
import { useOAuthCredentialAutoFill } from "@/routes/workflows/editor/hooks/useOAuthCredentialAutoFill";
import { PlusIcon } from "@radix-ui/react-icons";

type Props = {
  nodeId: string;
  value: string;
  onChange: (value: string) => void;
  requiredScopes: readonly string[];
  optional?: boolean;
  disabled?: boolean;
  ariaLabel?: string;
  showEmail?: boolean;
  additionalAccounts?: {
    options: Array<{ id: string; label: string; disabled?: boolean }>;
    selectedId?: string;
    onSelect: (id: string) => void;
  };
  // Offer only accounts the server reports as able to send mail, and never pick one for the user.
  gmailSendOnly?: boolean;
};

const ADVANCED_OPTION = "__advanced__";
const SETTINGS_OPTION = "__settings__";
const NONE_OPTION = "__none__";
const NATIVE_PREFIX = "__google_account__:";
const ADDITIONAL_PREFIX = "__connected_account__:";

function GoogleOAuthCredentialSelector({
  nodeId,
  value,
  onChange,
  requiredScopes,
  optional = false,
  gmailSendOnly = false,
  disabled = false,
  ariaLabel,
  showEmail = false,
  additionalAccounts,
}: Readonly<Props>) {
  const {
    credentials: allCredentials,
    isLoading,
    isFetching,
  } = useGoogleOAuthCredentials({ includeEmail: showEmail });
  const canAuthorize = useHasCapability("google_oauth.authorize");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const credentials = allCredentials.filter(
    (credential) =>
      isGoogleOAuthCredentialActive(credential) &&
      hasGoogleOAuthCredentialScopes(credential, requiredScopes) &&
      (!gmailSendOnly || credential.gmail_send_ready === true),
  );

  // If the value looks like a Jinja template, default to advanced mode
  const isTemplateValue = value.includes("{{") || value.includes("{%");
  const hasAdditionalSelection = additionalAccounts?.selectedId !== undefined;
  const useAdvanced =
    !gmailSendOnly &&
    !hasAdditionalSelection &&
    (showAdvanced || isTemplateValue);

  const hasCredentials = credentials.length > 0;
  const isKnownCredential = credentials.some((c) => c.id === value);
  // The saved id may point at a connection that still exists but is expired
  // ("needs reconnect"). Re-auth-in-place keeps the same id, so the block keeps
  // working once the account is reconnected — no need to repoint.
  const savedCredentialNeedsReconnect =
    !!value &&
    !isKnownCredential &&
    allCredentials.some(
      (c) => c.id === value && !isGoogleOAuthCredentialActive(c),
    );
  const savedCredentialCannotSend =
    gmailSendOnly &&
    !!value &&
    !isKnownCredential &&
    allCredentials.some(
      (c) => c.id === value && isGoogleOAuthCredentialActive(c),
    );
  const firstValidId = getDefaultGoogleOAuthCredentialId(credentials);
  const needsAutoFill =
    !disabled &&
    !optional &&
    !gmailSendOnly &&
    !showAdvanced &&
    !hasAdditionalSelection &&
    !value;

  useOAuthCredentialAutoFill({
    nodeId,
    field: "credentialId:google",
    value,
    firstValidId,
    needsAutoFill,
    isLoading,
    isFetching,
    onChange,
  });

  const handlePickerValueChange = (selected: string) => {
    if (disabled) return;
    if (selected === NONE_OPTION) {
      onChange("");
      return;
    }
    if (selected === ADVANCED_OPTION) {
      setShowAdvanced(true);
      if (hasAdditionalSelection) onChange("");
      return;
    }

    if (selected === SETTINGS_OPTION) {
      window.open("/integrations", "_blank");
      return;
    }

    if (additionalAccounts && selected.startsWith(ADDITIONAL_PREFIX)) {
      additionalAccounts.onSelect(selected.slice(ADDITIONAL_PREFIX.length));
      return;
    }
    onChange(
      additionalAccounts && selected.startsWith(NATIVE_PREFIX)
        ? selected.slice(NATIVE_PREFIX.length)
        : selected,
    );
  };

  const handleUseAccountPicker = () => {
    setShowAdvanced(false);

    // Clear templates so view can switch from advanced editor back to picker.
    if (isTemplateValue) {
      onChange("");
    }
  };

  if (isLoading) {
    return <Skeleton className="h-9 w-full" />;
  }

  return (
    <div className="space-y-2">
      {useAdvanced ? (
        <>
          <WorkflowBlockInputTextarea
            name="credentialId:google"
            nodeId={nodeId}
            value={value}
            onChange={onChange}
            placeholder="{{ google_credential_id }}"
            className="nopan text-xs"
            disabled={disabled}
            aria-label={ariaLabel}
          />
          <button
            type="button"
            disabled={disabled}
            onClick={handleUseAccountPicker}
            className="text-xs text-muted-foreground underline hover:text-tertiary-foreground"
          >
            Use account picker
          </button>
        </>
      ) : (
        <>
          {savedCredentialCannotSend ? (
            <p className="rounded-md border border-amber-300 bg-amber-100 px-2 py-1 text-[0.7rem] text-amber-700 dark:border-amber-600/40 dark:bg-amber-900/20 dark:text-amber-200">
              Saved Google account cannot send email. Enable sending for it on
              the{" "}
              <a
                href="/integrations"
                target="_blank"
                rel="noreferrer"
                className="underline"
              >
                Integrations
              </a>{" "}
              page, or pick another account.
            </p>
          ) : savedCredentialNeedsReconnect ? (
            <p className="rounded-md border border-amber-300 bg-amber-100 px-2 py-1 text-[0.7rem] text-amber-700 dark:border-amber-600/40 dark:bg-amber-900/20 dark:text-amber-200">
              Saved Google account needs to be reconnected. Reconnect it on the{" "}
              <a
                href="/integrations"
                target="_blank"
                rel="noreferrer"
                className="underline"
              >
                Integrations
              </a>{" "}
              page to keep this block working.
            </p>
          ) : value && hasCredentials && !isKnownCredential ? (
            <p className="rounded-md border border-amber-300 bg-amber-100 px-2 py-1 text-[0.7rem] text-amber-700 dark:border-amber-600/40 dark:bg-amber-900/20 dark:text-amber-200">
              Saved Google account is no longer connected. Pick another below.
            </p>
          ) : null}

          <Select
            value={
              hasAdditionalSelection
                ? `${ADDITIONAL_PREFIX}${additionalAccounts.selectedId}`
                : additionalAccounts && value
                  ? `${NATIVE_PREFIX}${value}`
                  : value
            }
            onValueChange={handlePickerValueChange}
            disabled={disabled}
          >
            <SelectTrigger className="nopan text-xs" aria-label={ariaLabel}>
              <SelectValue placeholder="Select a Google account" />
            </SelectTrigger>
            <SelectContent
              className={showEmail ? "ph-no-capture" : undefined}
              data-private={showEmail ? "" : undefined}
            >
              {optional || gmailSendOnly ? (
                <SelectItem value={NONE_OPTION}>No Google account</SelectItem>
              ) : null}

              {credentials.map((cred) => (
                <CustomSelectItem
                  key={cred.id}
                  value={
                    additionalAccounts ? `${NATIVE_PREFIX}${cred.id}` : cred.id
                  }
                >
                  <div className="space-y-0.5">
                    <p className="text-sm font-medium">
                      <SelectItemText>
                        {showEmail
                          ? cred.email_address || cred.credential_name
                          : cred.credential_name}
                      </SelectItemText>
                    </p>
                    <p className="text-xs text-muted-foreground">{cred.id}</p>
                  </div>
                </CustomSelectItem>
              ))}

              {additionalAccounts?.options.map((account) => (
                <SelectItem
                  key={account.id}
                  value={`${ADDITIONAL_PREFIX}${account.id}`}
                  disabled={account.disabled}
                >
                  {account.label}
                </SelectItem>
              ))}

              <SelectItem value={SETTINGS_OPTION} disabled={!canAuthorize}>
                <div className="flex items-center gap-2">
                  <PlusIcon className="size-4" />
                  <span>
                    {canAuthorize
                      ? "Connect new account"
                      : "Connect new account (admin required)"}
                  </span>
                </div>
              </SelectItem>

              {gmailSendOnly ? null : (
                <SelectItem value={ADVANCED_OPTION}>
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-xs">{"{{}}"}</span>
                    <span>Use template expression</span>
                  </div>
                </SelectItem>
              )}
            </SelectContent>
          </Select>
        </>
      )}
    </div>
  );
}

export { GoogleOAuthCredentialSelector };
