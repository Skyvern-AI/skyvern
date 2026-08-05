import { CodeIcon } from "@radix-ui/react-icons";
import { useEffect, useRef, useState } from "react";

import {
  CustomSelectItem,
  Select,
  SelectContent,
  SelectItemText,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
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
import { GOOGLE_GMAIL_REQUIRED_SCOPES } from "@/util/googleScopes";
import { MICROSOFT_MAIL_REQUIRED_SCOPES } from "@/util/microsoftScopes";
import { cn } from "@/util/utils";

type ConnectedEmailAccountPickerProps = {
  value: string;
  onChange: (value: string) => void;
  renderCustomInput: (props: {
    value: string;
    onChange: (value: string) => void;
  }) => React.ReactNode;
  disabled?: boolean;
};

type PickerMode = "dropdown" | "custom";

type EmailOption = {
  id: string;
  emailAddress: string;
  provider: "Gmail" | "Outlook";
  credentialName: string;
};

function optionHint(option: EmailOption): string {
  const credentialName = option.credentialName.trim();
  return credentialName.toLowerCase() === "default"
    ? option.provider
    : `${option.provider} · ${credentialName}`;
}

function initialPickerMode(
  value: string,
  valueMatchesOption: boolean,
  optionCount: number,
): PickerMode {
  return (value && !valueMatchesOption) || optionCount === 0
    ? "custom"
    : "dropdown";
}

function ConnectedEmailModeToggle({
  mode,
  disabled,
  onToggle,
}: {
  mode: PickerMode;
  disabled: boolean;
  onToggle: () => void;
}) {
  const label =
    mode === "dropdown" ? "Enter manually" : "Choose connected account";

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            disabled={disabled}
            aria-label={label}
            aria-pressed={mode === "custom"}
            onClick={onToggle}
            className={cn(
              "nopan inline-flex size-6 shrink-0 items-center justify-center rounded-md border border-input transition-colors disabled:cursor-not-allowed disabled:opacity-50",
              mode === "custom"
                ? "bg-muted text-foreground dark:bg-slate-700"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            <CodeIcon className="size-3.5" />
          </button>
        </TooltipTrigger>
        <TooltipContent>{label}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

function ConnectedEmailAccountPicker({
  value,
  onChange,
  renderCustomInput,
  disabled = false,
}: ConnectedEmailAccountPickerProps) {
  const google = useGoogleOAuthCredentials({ includeEmail: true });
  const microsoft = useMicrosoftOAuthCredentials({ includeEmail: true });

  const googleCredentials = google.credentials.filter(
    (credential) =>
      isGoogleOAuthCredentialActive(credential) &&
      hasGoogleOAuthCredentialScopes(credential, GOOGLE_GMAIL_REQUIRED_SCOPES),
  );
  const microsoftCredentials = microsoft.credentials.filter(
    (credential) =>
      isMicrosoftOAuthCredentialActive(credential) &&
      hasMicrosoftOAuthCredentialScopes(
        credential,
        MICROSOFT_MAIL_REQUIRED_SCOPES,
      ),
  );
  const options: EmailOption[] = [
    ...googleCredentials.flatMap((credential) => {
      const emailAddress = credential.email_address?.trim();
      return emailAddress
        ? [
            {
              id: `google:${credential.id}`,
              emailAddress,
              provider: "Gmail" as const,
              credentialName: credential.credential_name,
            },
          ]
        : [];
    }),
    ...microsoftCredentials.flatMap((credential) => {
      const emailAddress = credential.email_address?.trim();
      return emailAddress
        ? [
            {
              id: `microsoft:${credential.id}`,
              emailAddress,
              provider: "Outlook" as const,
              credentialName: credential.credential_name,
            },
          ]
        : [];
    }),
  ];
  const isLoading = google.isLoading || microsoft.isLoading;
  const hasLoadError = Boolean(google.error || microsoft.error);
  const valueMatchesOption = options.some(
    (option) => option.emailAddress === value,
  );
  const optionListKey = JSON.stringify(
    options.map((option) => [option.id, option.emailAddress]),
  );
  const [mode, setMode] = useState<PickerMode | null>(() =>
    isLoading
      ? null
      : initialPickerMode(value, valueMatchesOption, options.length),
  );
  const previousValueRef = useRef(value);
  const previousOptionListKeyRef = useRef(optionListKey);
  useEffect(() => {
    const valueChanged = previousValueRef.current !== value;
    const optionListChanged =
      previousOptionListKeyRef.current !== optionListKey;
    previousValueRef.current = value;
    previousOptionListKeyRef.current = optionListKey;
    if (mode === null && !isLoading) {
      setMode(initialPickerMode(value, valueMatchesOption, options.length));
      return;
    }
    if (
      mode === "dropdown" &&
      !isLoading &&
      (valueChanged || optionListChanged) &&
      value &&
      !valueMatchesOption
    ) {
      setMode("custom");
    }
  }, [
    isLoading,
    mode,
    optionListKey,
    options.length,
    value,
    valueMatchesOption,
  ]);
  const renderedMode = mode ?? "dropdown";
  const eligibleAccountCount =
    googleCredentials.length + microsoftCredentials.length;
  const emptyHint =
    options.length === 0 && !isLoading
      ? hasLoadError
        ? "Couldn't load connected accounts — enter the address manually"
        : eligibleAccountCount === 0
          ? "Connect Gmail or Outlook to pick an account"
          : "Reconnect Gmail or Outlook to pick an account"
      : null;

  return (
    <div className="min-w-0 space-y-2">
      <div className="flex justify-end">
        <ConnectedEmailModeToggle
          mode={renderedMode}
          disabled={disabled}
          onToggle={() => {
            if (renderedMode === "dropdown") {
              setMode("custom");
            } else if (!value || valueMatchesOption) {
              setMode("dropdown");
            }
          }}
        />
      </div>
      {renderedMode === "custom" ? (
        renderCustomInput({
          value,
          onChange: disabled ? () => undefined : onChange,
        })
      ) : isLoading || mode === null ? (
        <Select disabled>
          <SelectTrigger
            aria-label="Connected email account"
            className="nopan text-xs"
          >
            <SelectValue placeholder="Loading connected accounts..." />
          </SelectTrigger>
        </Select>
      ) : (
        <Select
          disabled={disabled}
          value={valueMatchesOption ? value : ""}
          onValueChange={onChange}
        >
          <SelectTrigger
            aria-label="Connected email account"
            className="nopan text-xs"
          >
            <SelectValue placeholder="Select a connected account" />
          </SelectTrigger>
          <SelectContent>
            {options.map((option) => (
              <CustomSelectItem
                key={option.id}
                value={option.emailAddress}
                className="pr-8"
              >
                <div className="flex min-w-0 flex-1 items-center justify-between gap-3">
                  <span className="min-w-0 truncate font-medium">
                    <SelectItemText>{option.emailAddress}</SelectItemText>
                  </span>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {optionHint(option)}
                  </span>
                </div>
              </CustomSelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      {emptyHint ? (
        <p className="text-xs text-muted-foreground">
          {hasLoadError ? (
            emptyHint
          ) : (
            <a
              href="/integrations"
              className="underline underline-offset-2 hover:text-foreground"
            >
              {emptyHint}
            </a>
          )}
        </p>
      ) : null}
    </div>
  );
}

export { ConnectedEmailAccountPicker, type ConnectedEmailAccountPickerProps };
