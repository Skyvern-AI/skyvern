import { useState } from "react";
import { PlusIcon } from "@radix-ui/react-icons";

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
  getDefaultMicrosoftOAuthCredentialId,
  hasMicrosoftOAuthCredentialScopes,
  isMicrosoftOAuthCredentialActive,
  useMicrosoftOAuthCredentials,
} from "@/hooks/useMicrosoftOAuthCredentials";
import { MICROSOFT_MAIL_REQUIRED_SCOPES } from "@/util/microsoftScopes";
import { useHasCapability } from "@/store/PageSlots";
import { useWorkflowScopeReadOnly } from "@/routes/workflows/editor/WorkflowScopeContext";
import { useOAuthCredentialAutoFill } from "@/routes/workflows/editor/hooks/useOAuthCredentialAutoFill";
export { MICROSOFT_MAIL_REQUIRED_SCOPES };

type Props = {
  nodeId: string;
  value: string;
  onChange: (value: string) => void;
  requiredScopes: readonly string[];
  editable?: boolean;
  integrationName?: string;
};

const ADVANCED_OPTION = "__advanced__";
const CONNECT_OPTION = "__connect__";

function MicrosoftOAuthCredentialSelector({
  nodeId,
  value,
  onChange,
  requiredScopes,
  editable = true,
  integrationName = "Microsoft",
}: Readonly<Props>) {
  const {
    credentials: allCredentials,
    isLoading,
    isFetching,
    error,
  } = useMicrosoftOAuthCredentials();
  const canAuthorize = useHasCapability("microsoft_oauth.authorize");
  const scopeReadOnly = useWorkflowScopeReadOnly();
  const canEdit = editable && !scopeReadOnly;
  const isPowerBI = integrationName === "Power BI";
  const [showAdvanced, setShowAdvanced] = useState(false);
  const isTemplateValue = value.includes("{{") || value.includes("{%");
  const useAdvanced = showAdvanced || isTemplateValue;
  const credentials = allCredentials.filter(
    (credential) =>
      isMicrosoftOAuthCredentialActive(credential) &&
      hasMicrosoftOAuthCredentialScopes(credential, requiredScopes),
  );
  const isKnownCredential = credentials.some(
    (credential) => credential.id === value,
  );
  const connectLabel =
    integrationName === "Microsoft"
      ? canAuthorize
        ? "Connect new account"
        : "Connect new account (admin required)"
      : canAuthorize && canEdit
        ? `Connect ${integrationName} account`
        : "Connect account (admin and edit access required)";
  const savedCredential = allCredentials.find(
    (credential) => credential.id === value,
  );
  const firstValidId = getDefaultMicrosoftOAuthCredentialId(credentials);

  useOAuthCredentialAutoFill({
    nodeId,
    field: "credentialId:microsoft",
    value,
    firstValidId,
    needsAutoFill: !value && canEdit,
    isLoading,
    isFetching,
    onChange,
  });

  const handlePickerValueChange = (selected: string) => {
    if (selected === CONNECT_OPTION) {
      window.open("/integrations", "_blank", "noopener,noreferrer");
      return;
    }
    if (selected === ADVANCED_OPTION) {
      setShowAdvanced(true);
      return;
    }
    onChange(selected);
  };

  const handleUseAccountPicker = () => {
    setShowAdvanced(false);
    if (isTemplateValue) onChange("");
  };

  if (isLoading) {
    return <Skeleton className="h-9 w-full" />;
  }

  return (
    <div className="space-y-2">
      {error ? (
        <p role="alert" className="text-xs text-destructive">
          Could not load {integrationName} connections. Retry or manage
          connections on the{" "}
          <a href="/integrations" className="underline">
            Integrations
          </a>{" "}
          page.
        </p>
      ) : null}
      {value && !isKnownCredential && !useAdvanced ? (
        <p role="status" className="text-xs text-amber-700 dark:text-amber-200">
          {savedCredential
            ? !isMicrosoftOAuthCredentialActive(savedCredential)
              ? `This ${integrationName} connection needs to be reconnected.`
              : `This Microsoft connection does not have ${integrationName} access.`
            : `This saved ${integrationName} connection is unavailable to your account.`}{" "}
          <a
            href="/integrations"
            target="_blank"
            rel="noreferrer"
            className="underline"
          >
            Manage connections
          </a>
          .
        </p>
      ) : null}
      {!error && !value && credentials.length === 0 && !useAdvanced ? (
        <p role="status" className="text-xs text-muted-foreground">
          No active {integrationName} connections are available. Connect an
          account from the{" "}
          <a
            href="/integrations"
            target="_blank"
            rel="noreferrer"
            className="underline"
          >
            Integrations
          </a>{" "}
          page.
        </p>
      ) : null}

      {useAdvanced ? (
        <>
          <WorkflowBlockInputTextarea
            name="credentialId:microsoft"
            nodeId={nodeId}
            value={value}
            onChange={onChange}
            disabled={!canEdit}
            placeholder="{{ microsoft_credential_id }}"
            className="nopan text-xs"
          />
          <button
            type="button"
            onClick={handleUseAccountPicker}
            disabled={!canEdit}
            className="text-xs text-muted-foreground underline hover:text-tertiary-foreground disabled:cursor-not-allowed disabled:opacity-50"
          >
            Use account picker
          </button>
        </>
      ) : (
        <Select
          value={isKnownCredential ? value : ""}
          onValueChange={handlePickerValueChange}
          disabled={!canEdit || Boolean(error)}
        >
          <SelectTrigger className="nopan text-xs">
            <SelectValue
              placeholder={`Select a ${integrationName} connection`}
            />
          </SelectTrigger>
          <SelectContent>
            {credentials.map((credential) => (
              <CustomSelectItem key={credential.id} value={credential.id}>
                <div className="space-y-0.5">
                  <p className="text-sm font-medium">
                    <SelectItemText>
                      {credential.credential_name}
                    </SelectItemText>
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {credential.id}
                  </p>
                </div>
              </CustomSelectItem>
            ))}
            <SelectItem
              value={CONNECT_OPTION}
              disabled={!canAuthorize || !canEdit}
            >
              <div className="flex items-center gap-2">
                <PlusIcon className="size-4" />
                <span>{connectLabel}</span>
              </div>
            </SelectItem>
            {!isPowerBI ? (
              <SelectItem value={ADVANCED_OPTION} disabled={!canEdit}>
                <span className="font-mono text-xs">{"{{}}"}</span> Use template
                expression
              </SelectItem>
            ) : null}
          </SelectContent>
        </Select>
      )}
    </div>
  );
}

export { MicrosoftOAuthCredentialSelector };
