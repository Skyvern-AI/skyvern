import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { toast } from "@/components/ui/use-toast";
import CloudContext from "@/store/CloudContext";
import { Cross2Icon } from "@radix-ui/react-icons";
import { useContext, useState } from "react";
import { CredentialParameterSourceSelector } from "../../components/CredentialParameterSourceSelector";
import { SourceParameterKeySelector } from "../../components/SourceParameterKeySelector";
import {
  WorkflowEditorParameterType,
  WorkflowParameterValueType,
} from "../../types/workflowTypes";
import { WorkflowParameterInput } from "../../WorkflowParameterInput";
import {
  parameterIsBitwardenCredential,
  parameterIsSkyvernCredential,
  parameterIsOnePasswordCredential,
  ParametersState,
  parameterIsAzureVaultCredential,
} from "../types";
import { getDefaultValueForParameterType } from "../workflowEditorUtils";
import { validateBitwardenLoginCredential } from "./util";
import { HelpTooltip } from "@/components/HelpTooltip";

type Props = {
  type: WorkflowEditorParameterType;
  onClose: () => void;
  onSave: (value: ParametersState[number]) => void;
  initialValues?: ParametersState[number];
};

const workflowParameterTypeOptions = [
  { label: "string", value: WorkflowParameterValueType.String },
  { label: "credential", value: "credential" },
  { label: "float", value: WorkflowParameterValueType.Float },
  { label: "integer", value: WorkflowParameterValueType.Integer },
  { label: "boolean", value: WorkflowParameterValueType.Boolean },
  { label: "file", value: WorkflowParameterValueType.FileURL },
  { label: "JSON", value: WorkflowParameterValueType.JSON },
];

type CredentialDataType = "password" | "secret" | "creditCard";
type CredentialSource = "bitwarden" | "skyvern" | "onepassword" | "azurevault";

// When selecting from the Value Type dropdown, "credential" is a special value that triggers
// credential-specific UI. This is separate from WorkflowParameterValueType which only includes
// data types like string, integer, etc.
type ParameterTypeSelection = WorkflowParameterValueType | "credential";

// Determine available sources based on credential data type
function getAvailableSourcesForDataType(
  dataType: CredentialDataType,
  isCloud: boolean,
): Array<{ value: CredentialSource; label: string }> {
  switch (dataType) {
    case "password":
      return [
        ...(isCloud ? [{ value: "skyvern" as const, label: "Skyvern" }] : []),
        { value: "bitwarden" as const, label: "Bitwarden" },
        { value: "onepassword" as const, label: "1Password" },
        { value: "azurevault" as const, label: "Azure Key Vault" },
      ];
    case "secret":
      return [
        ...(isCloud ? [{ value: "skyvern" as const, label: "Skyvern" }] : []),
        { value: "bitwarden" as const, label: "Bitwarden" },
      ];
    case "creditCard":
      return [
        ...(isCloud ? [{ value: "skyvern" as const, label: "Skyvern" }] : []),
        { value: "bitwarden" as const, label: "Bitwarden" },
        { value: "onepassword" as const, label: "1Password" },
      ];
  }
}

function header(
  type: WorkflowEditorParameterType,
  isEdit: boolean,
  isCredentialSelected: boolean,
) {
  const prefix = isEdit ? "Edit" : "Add";
  if (type === "workflow" && !isEdit) {
    // Unified add mode
    return `${prefix} Parameter`;
  }
  if (type === "workflow") {
    return `${prefix} Input Parameter`;
  }
  if (type === "credential" || (!isEdit && isCredentialSelected)) {
    return `${prefix} Credential Parameter`;
  }
  return `${prefix} Context Parameter`;
}

/**
 * Validates that a parameter key is a valid Python/Jinja2 identifier.
 * Parameter keys are used in Jinja2 templates, so they must be valid identifiers.
 * Returns an error message if invalid, or null if valid.
 */
function validateParameterKey(key: string): string | null {
  if (!key) return null; // Empty key is handled separately

  // Check for whitespace
  if (/\s/.test(key)) {
    return "Key cannot contain whitespace characters. Consider using underscores (_) instead.";
  }

  // Check if it's a valid Python identifier:
  // - Must start with a letter (a-z, A-Z) or underscore (_)
  // - Can only contain letters, digits (0-9), and underscores
  const validIdentifierRegex = /^[a-zA-Z_][a-zA-Z0-9_]*$/;
  if (!validIdentifierRegex.test(key)) {
    if (/^[0-9]/.test(key)) {
      return "Key cannot start with a digit. Parameter keys must start with a letter or underscore.";
    }
    if (key.includes("/")) {
      return "Key cannot contain '/' characters. Use underscores instead (e.g., 'State_or_Province' instead of 'State/Province').";
    }
    if (key.includes("-")) {
      return "Key cannot contain '-' characters. Use underscores instead (e.g., 'my_parameter' instead of 'my-parameter').";
    }
    if (key.includes(".")) {
      return "Key cannot contain '.' characters. Use underscores instead.";
    }
    return "Key must be a valid identifier (only letters, digits, and underscores; cannot start with a digit).";
  }

  return null;
}

// Helper to detect initial credential data type from existing parameter
function detectInitialCredentialDataType(
  initialValues: ParametersState[number] | undefined,
): CredentialDataType {
  if (!initialValues) return "password";
  if (initialValues.parameterType === "secret") return "secret";
  if (initialValues.parameterType === "creditCardData") return "creditCard";
  return "password";
}

// Helper to detect initial credential source from existing parameter
function detectInitialCredentialSource(
  initialValues: ParametersState[number] | undefined,
  isCloud: boolean,
): CredentialSource {
  if (!initialValues) return isCloud ? "skyvern" : "bitwarden";

  if (initialValues.parameterType === "secret") return "bitwarden";
  if (initialValues.parameterType === "creditCardData") return "bitwarden";
  if (initialValues.parameterType === "onepassword") return "onepassword";

  if (initialValues.parameterType === "credential") {
    if (parameterIsSkyvernCredential(initialValues)) return "skyvern";
    if (parameterIsBitwardenCredential(initialValues)) return "bitwarden";
    if (parameterIsAzureVaultCredential(initialValues)) return "azurevault";
  }

  return isCloud ? "skyvern" : "bitwarden";
}

function WorkflowParameterEditPanel({
  type,
  onClose,
  onSave,
  initialValues,
}: Props) {
  const reservedKeys = [
    "current_item",
    "current_value",
    "current_index",
    "current_date",
    "workflow_title",
    "workflow_id",
    "workflow_permanent_id",
    "workflow_run_id",
    "workflow_run_outputs",
    "workflow_run_summary",
  ];
  const isCloud = useContext(CloudContext);
  const isEditMode = !!initialValues;
  const [key, setKey] = useState(initialValues?.key ?? "");
  const keyValidationError = validateParameterKey(key);

  // Detect initial values for backward compatibility
  const isBitwardenCredential =
    initialValues?.parameterType === "credential" &&
    parameterIsBitwardenCredential(initialValues);
  const isSkyvernCredential =
    initialValues?.parameterType === "credential" &&
    parameterIsSkyvernCredential(initialValues);
  const isOnePasswordCredential =
    initialValues?.parameterType === "onepassword" &&
    parameterIsOnePasswordCredential(initialValues);
  const isAzureVaultCredential =
    initialValues?.parameterType === "credential" &&
    parameterIsAzureVaultCredential(initialValues);

  // New unified credential state
  const [credentialDataType, setCredentialDataType] =
    useState<CredentialDataType>(
      detectInitialCredentialDataType(initialValues),
    );
  const [credentialSource, setCredentialSource] = useState<CredentialSource>(
    detectInitialCredentialSource(initialValues, isCloud),
  );

  const [urlParameterKey, setUrlParameterKey] = useState(
    isBitwardenCredential ? initialValues?.urlParameterKey ?? "" : "",
  );
  const [description, setDescription] = useState(
    initialValues?.description ?? "",
  );
  const [bitwardenCollectionId, setBitwardenCollectionId] = useState(
    isBitwardenCredential ||
      initialValues?.parameterType === "secret" ||
      initialValues?.parameterType === "creditCardData"
      ? initialValues?.collectionId ?? ""
      : "",
  );
  const [parameterType, setParameterType] = useState<ParameterTypeSelection>(
    type === "credential"
      ? "credential"
      : initialValues?.parameterType === "workflow"
        ? initialValues.dataType
        : "string",
  );

  const [defaultValueState, setDefaultValueState] = useState<{
    hasDefaultValue: boolean;
    defaultValue: unknown;
  }>(
    initialValues?.parameterType === "workflow"
      ? {
          hasDefaultValue: initialValues.defaultValue !== null,
          defaultValue: initialValues.defaultValue ?? null,
        }
      : {
          hasDefaultValue: false,
          defaultValue: null,
        },
  );

  const [sourceParameterKey, setSourceParameterKey] = useState<
    string | undefined
  >(
    initialValues?.parameterType === "context"
      ? initialValues.sourceParameterKey
      : undefined,
  );

  const [identityKey, setIdentityKey] = useState(
    initialValues?.parameterType === "secret" ? initialValues.identityKey : "",
  );

  const [identityFields, setIdentityFields] = useState(
    initialValues?.parameterType === "secret"
      ? initialValues.identityFields.join(", ")
      : "",
  );

  const [sensitiveInformationItemId, setSensitiveInformationItemId] = useState(
    initialValues?.parameterType === "creditCardData"
      ? initialValues.itemId
      : "",
  );

  const [credentialId, setCredentialId] = useState(
    isSkyvernCredential ? initialValues.credentialId : "",
  );
  const [opVaultId, setOpVaultId] = useState(
    isOnePasswordCredential ? initialValues.vaultId : "",
  );
  const [opItemId, setOpItemId] = useState(
    isOnePasswordCredential ? initialValues.itemId : "",
  );

  const [bitwardenLoginCredentialItemId, setBitwardenLoginCredentialItemId] =
    useState(isBitwardenCredential ? initialValues?.itemId ?? "" : "");

  const [azureVaultName, setAzureVaultName] = useState(
    isAzureVaultCredential ? initialValues.vaultName : "",
  );
  const [azureUsernameKey, setAzureUsernameKey] = useState(
    isAzureVaultCredential ? initialValues.usernameKey : "",
  );
  const [azurePasswordKey, setAzurePasswordKey] = useState(
    isAzureVaultCredential ? initialValues.passwordKey : "",
  );
  const [azureTotpSecretKey, setAzureTotpKey] = useState(
    isAzureVaultCredential ? initialValues.totpSecretKey ?? "" : "",
  );

  // Handle credential data type change - reset source to first available
  const handleCredentialDataTypeChange = (newDataType: CredentialDataType) => {
    setCredentialDataType(newDataType);
    const availableSources = getAvailableSourcesForDataType(
      newDataType,
      isCloud,
    );
    if (!availableSources.find((s) => s.value === credentialSource)) {
      setCredentialSource(availableSources[0]?.value ?? "bitwarden");
    }
  };

  const availableSources = getAvailableSourcesForDataType(
    credentialDataType,
    isCloud,
  );

  // Check if we're in unified add mode and credential is selected
  const isCredentialSelected = parameterType === "credential";
  const showCredentialFields =
    type === "credential" ||
    (type === "workflow" && !isEditMode && isCredentialSelected);

  // Determine what fields to show based on credential data type and source
  const showBitwardenPasswordFields =
    showCredentialFields &&
    credentialDataType === "password" &&
    credentialSource === "bitwarden";
  const showBitwardenSecretFields =
    showCredentialFields &&
    credentialDataType === "secret" &&
    credentialSource === "bitwarden";
  const showBitwardenCreditCardFields =
    showCredentialFields &&
    credentialDataType === "creditCard" &&
    credentialSource === "bitwarden";
  const showOnePasswordFields =
    showCredentialFields && credentialSource === "onepassword";
  const showAzureVaultFields =
    showCredentialFields && credentialSource === "azurevault";
  const showSkyvernCredentialSelector =
    showCredentialFields && credentialSource === "skyvern" && isCloud;

  return (
    <ScrollArea>
      <ScrollAreaViewport className="max-h-[500px]">
        <div className="space-y-4 p-1 px-4">
          <header className="flex items-center justify-between">
            <span>{header(type, isEditMode, isCredentialSelected)}</span>
            <Cross2Icon className="h-6 w-6 cursor-pointer" onClick={onClose} />
          </header>
          <div className="space-y-1">
            <Label className="text-xs text-slate-300">Key</Label>
            <Input value={key} onChange={(e) => setKey(e.target.value)} />
            {keyValidationError && (
              <p className="text-xs text-destructive">{keyValidationError}</p>
            )}
          </div>
          <div className="space-y-1">
            <Label className="text-xs text-slate-300">Description</Label>
            <Input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          {type === "workflow" && (
            <>
              <div className="space-y-1">
                <Label className="text-xs">Value Type</Label>
                <Select
                  value={parameterType}
                  onValueChange={(value) => {
                    const newValue = value as ParameterTypeSelection;
                    const wasCredential = parameterType === "credential";
                    const isNowCredential = newValue === "credential";

                    setParameterType(newValue);

                    // Clear credential-specific state when switching away from credential
                    // to prevent stale data if user switches back
                    if (wasCredential && !isNowCredential) {
                      setCredentialId("");
                      setCredentialDataType("password");
                      setCredentialSource(isCloud ? "skyvern" : "bitwarden");
                      setBitwardenLoginCredentialItemId("");
                      setBitwardenCollectionId("");
                      setUrlParameterKey("");
                      setIdentityKey("");
                      setIdentityFields("");
                      setSensitiveInformationItemId("");
                      setOpVaultId("");
                      setOpItemId("");
                      setAzureVaultName("");
                      setAzureUsernameKey("");
                      setAzurePasswordKey("");
                      setAzureTotpKey("");
                    }

                    // Clear default value state when switching to credential type
                    // since credentials don't use default values
                    if (!wasCredential && isNowCredential) {
                      setDefaultValueState({
                        hasDefaultValue: false,
                        defaultValue: null,
                      });
                    }

                    if (!isNowCredential) {
                      setDefaultValueState((state) => {
                        return {
                          ...state,
                          defaultValue: getDefaultValueForParameterType(
                            newValue as WorkflowParameterValueType,
                          ),
                        };
                      });
                    }
                  }}
                >
                  <SelectTrigger className="w-full">
                    <SelectValue placeholder="Select a type" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      {workflowParameterTypeOptions
                        .filter((option) => {
                          // In edit mode, don't show credential option
                          if (isEditMode && option.value === "credential") {
                            return false;
                          }
                          return true;
                        })
                        .map((option) => (
                          <SelectItem key={option.value} value={option.value}>
                            {option.label}
                          </SelectItem>
                        ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </div>
              {/* Default value section - only for non-credential types */}
              {!isCredentialSelected && (
                <div className="space-y-4">
                  <div className="flex items-center gap-2">
                    <Checkbox
                      checked={defaultValueState.hasDefaultValue}
                      onCheckedChange={(checked) => {
                        if (!checked) {
                          setDefaultValueState({
                            hasDefaultValue: false,
                            defaultValue: null,
                          });
                          return;
                        }
                        setDefaultValueState({
                          hasDefaultValue: true,
                          defaultValue: getDefaultValueForParameterType(
                            parameterType as WorkflowParameterValueType,
                          ),
                        });
                      }}
                    />
                    <Label className="text-xs text-slate-300">
                      Use Default Value
                    </Label>
                  </div>
                  {defaultValueState.hasDefaultValue && (
                    <WorkflowParameterInput
                      onChange={(value) => {
                        if (
                          parameterType === "file_url" &&
                          typeof value === "object" &&
                          value &&
                          "s3uri" in value
                        ) {
                          setDefaultValueState((state) => {
                            return {
                              ...state,
                              defaultValue: value.s3uri,
                            };
                          });
                          return;
                        }
                        setDefaultValueState((state) => {
                          return {
                            ...state,
                            defaultValue: value,
                          };
                        });
                      }}
                      type={parameterType as WorkflowParameterValueType}
                      value={defaultValueState.defaultValue}
                    />
                  )}
                </div>
              )}
            </>
          )}

          {/* Credential Parameter - Unified Flow */}
          {showCredentialFields && (
            <>
              {/* Step 1: Credential Type */}
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">
                    Credential Type
                  </Label>
                  <HelpTooltip content="Select the type of credential you want to use. Password for login credentials, Secret for sensitive data fields, Credit Card for payment information." />
                </div>
                <Select
                  value={credentialDataType}
                  onValueChange={(value) =>
                    handleCredentialDataTypeChange(value as CredentialDataType)
                  }
                >
                  <SelectTrigger className="w-full">
                    <SelectValue placeholder="Select credential type" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      <SelectItem value="password">Password</SelectItem>
                      <SelectItem value="secret">Secret</SelectItem>
                      <SelectItem value="creditCard">Credit Card</SelectItem>
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </div>

              {/* Step 2: Source */}
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">Source</Label>
                  <HelpTooltip content="Select where your credentials are stored. Skyvern uses managed credentials, while Bitwarden, 1Password, and Azure Key Vault connect directly to your vault." />
                </div>
                <Select
                  value={credentialSource}
                  onValueChange={(value) =>
                    setCredentialSource(value as CredentialSource)
                  }
                >
                  <SelectTrigger className="w-full">
                    <SelectValue placeholder="Select source" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      {availableSources.map((source) => (
                        <SelectItem key={source.value} value={source.value}>
                          {source.label}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </div>
            </>
          )}

          {/* Bitwarden Password Fields */}
          {showBitwardenPasswordFields && (
            <>
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">
                    URL Parameter Key
                  </Label>
                  <HelpTooltip content="Optional. The workflow parameter key that holds the URL. If provided, Skyvern will match the credential based on this URL." />
                </div>
                <Input
                  value={urlParameterKey}
                  onChange={(e) => setUrlParameterKey(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">
                    Bitwarden Collection ID
                  </Label>
                  <HelpTooltip content="The Bitwarden collection ID. You can find this in the URL when viewing a collection in Bitwarden (e.g., https://vault.bitwarden.com/#/organizations/.../collections/[COLLECTION_ID])." />
                </div>
                <Input
                  value={bitwardenCollectionId}
                  onChange={(e) => setBitwardenCollectionId(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">
                    Bitwarden Item ID
                  </Label>
                  <HelpTooltip content="The Bitwarden item ID. You can find this in the URL when viewing the item in Bitwarden (e.g., https://vault.bitwarden.com/#/vault?itemId=[ITEM_ID])." />
                </div>
                <Input
                  value={bitwardenLoginCredentialItemId}
                  onChange={(e) =>
                    setBitwardenLoginCredentialItemId(e.target.value)
                  }
                />
              </div>
            </>
          )}

          {/* Bitwarden Secret Fields */}
          {showBitwardenSecretFields && (
            <>
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">
                    Bitwarden Collection ID
                  </Label>
                  <HelpTooltip content="Required. The Bitwarden collection ID containing the identity item. You can find this in the URL when viewing a collection in Bitwarden." />
                </div>
                <Input
                  value={bitwardenCollectionId}
                  onChange={(e) => setBitwardenCollectionId(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">Identity Key</Label>
                  <HelpTooltip content="The key used to identify which identity to use from Bitwarden (e.g., the identity name or a custom identifier)." />
                </div>
                <Input
                  value={identityKey}
                  onChange={(e) => setIdentityKey(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">
                    Identity Fields
                  </Label>
                  <HelpTooltip content="Comma-separated list of field names to extract from the Bitwarden identity (e.g., 'ssn, address, phone')." />
                </div>
                <Input
                  value={identityFields}
                  onChange={(e) => setIdentityFields(e.target.value)}
                  placeholder="field1, field2, field3"
                />
              </div>
            </>
          )}

          {/* Bitwarden Credit Card Fields */}
          {showBitwardenCreditCardFields && (
            <>
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">
                    Bitwarden Collection ID
                  </Label>
                  <HelpTooltip content="Required. The Bitwarden collection ID containing the credit card. You can find this in the URL when viewing a collection in Bitwarden." />
                </div>
                <Input
                  value={bitwardenCollectionId}
                  onChange={(e) => setBitwardenCollectionId(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">
                    Bitwarden Item ID
                  </Label>
                  <HelpTooltip content="Required. The Bitwarden item ID of the credit card. You can find this in the URL when viewing the item in Bitwarden." />
                </div>
                <Input
                  value={sensitiveInformationItemId}
                  onChange={(e) =>
                    setSensitiveInformationItemId(e.target.value)
                  }
                />
              </div>
            </>
          )}

          {/* 1Password Fields */}
          {showOnePasswordFields && (
            <>
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">
                    1Password Vault ID
                  </Label>
                  <HelpTooltip content="You can find the Vault ID in the URL when viewing the vault in 1Password on the web (e.g., https://my.1password.com/vaults/[VAULT_ID])." />
                </div>
                <Input
                  value={opVaultId}
                  onChange={(e) => setOpVaultId(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">
                    1Password Item ID
                  </Label>
                  <HelpTooltip content="You can find the Item ID in the URL when viewing the item in 1Password on the web. Supports all item types: Logins, Passwords, Credit Cards, Secure Notes, and more." />
                </div>
                <Input
                  value={opItemId}
                  onChange={(e) => setOpItemId(e.target.value)}
                />
              </div>
              {credentialDataType === "creditCard" && (
                <div className="rounded-md bg-slate-800 p-2">
                  <div className="space-y-1 text-xs text-slate-400">
                    Credit Cards: Due to a 1Password limitation, add the
                    expiration date as a separate text field named "Expire Date"
                    in the format MM/YYYY (e.g. 09/2027).
                  </div>
                </div>
              )}
            </>
          )}

          {/* Azure Key Vault Fields */}
          {showAzureVaultFields && (
            <>
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">
                    Azure Key Vault Name
                  </Label>
                  <HelpTooltip content="The name of your Azure Key Vault instance (e.g., 'my-company-vault'). This is the name you see in the Azure portal." />
                </div>
                <Input
                  value={azureVaultName}
                  onChange={(e) => setAzureVaultName(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">
                    Azure Username Secret Key
                  </Label>
                  <HelpTooltip content="The secret name in Azure Key Vault that stores the username (e.g., 'my-app-username')." />
                </div>
                <Input
                  autoComplete="off"
                  value={azureUsernameKey}
                  onChange={(e) => setAzureUsernameKey(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">
                    Azure Password Secret Key
                  </Label>
                  <HelpTooltip content="The secret name in Azure Key Vault that stores the password (e.g., 'my-app-password')." />
                </div>
                <Input
                  value={azurePasswordKey}
                  onChange={(e) => setAzurePasswordKey(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">
                    Azure TOTP Secret Key
                  </Label>
                  <HelpTooltip content="Optional. The secret name in Azure Key Vault that stores the TOTP secret for two-factor authentication." />
                </div>
                <Input
                  value={azureTotpSecretKey}
                  onChange={(e) => setAzureTotpKey(e.target.value)}
                />
              </div>
            </>
          )}

          {/* Skyvern Managed Credential Selector */}
          {showSkyvernCredentialSelector && (
            <div className="space-y-1">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">
                  Skyvern Credential
                </Label>
                <HelpTooltip content="Select a credential from your Skyvern credential store. These are managed credentials you've previously added to Skyvern." />
              </div>
              <CredentialParameterSourceSelector
                value={credentialId}
                onChange={(value) => setCredentialId(value)}
              />
            </div>
          )}

          {type === "context" && (
            <div className="space-y-1">
              <Label className="text-xs text-slate-300">Source Parameter</Label>
              <SourceParameterKeySelector
                value={sourceParameterKey}
                onChange={setSourceParameterKey}
              />
            </div>
          )}

          <div className="flex justify-end">
            <Button
              onClick={() => {
                if (!key) {
                  toast({
                    variant: "destructive",
                    title: "Failed to save parameter",
                    description: "Key is required",
                  });
                  return;
                }
                if (keyValidationError) {
                  toast({
                    variant: "destructive",
                    title: "Failed to save parameter",
                    description: keyValidationError,
                  });
                  return;
                }
                if (!isEditMode && reservedKeys.includes(key)) {
                  toast({
                    variant: "destructive",
                    title: "Failed to add parameter",
                    description: `${key} is reserved, please use another key`,
                  });
                  return;
                }

                // Handle workflow parameters (non-credential)
                if (type === "workflow" && !isCredentialSelected) {
                  if (
                    parameterType === "json" &&
                    typeof defaultValueState.defaultValue === "string"
                  ) {
                    try {
                      JSON.parse(defaultValueState.defaultValue);
                    } catch (e) {
                      toast({
                        variant: "destructive",
                        title: "Failed to save parameter",
                        description: "Invalid JSON for default value",
                      });
                      return;
                    }
                  }
                  let defaultValue = defaultValueState.defaultValue;

                  // Handle JSON parsing
                  if (
                    parameterType === "json" &&
                    typeof defaultValueState.defaultValue === "string"
                  ) {
                    defaultValue = JSON.parse(defaultValueState.defaultValue);
                  }
                  // Convert boolean to string for backend storage
                  else if (
                    parameterType === "boolean" &&
                    typeof defaultValueState.defaultValue === "boolean"
                  ) {
                    defaultValue = String(defaultValueState.defaultValue);
                  }
                  // Convert numeric defaults to strings for backend storage
                  else if (
                    (parameterType === "integer" ||
                      parameterType === "float") &&
                    (typeof defaultValueState.defaultValue === "number" ||
                      typeof defaultValueState.defaultValue === "string")
                  ) {
                    defaultValue = String(defaultValueState.defaultValue);
                  }

                  onSave({
                    key,
                    parameterType: "workflow",
                    dataType: parameterType as WorkflowParameterValueType,
                    description,
                    defaultValue: defaultValueState.hasDefaultValue
                      ? defaultValue
                      : null,
                  });
                  return;
                }

                // Handle context parameters
                if (type === "context") {
                  if (!sourceParameterKey) {
                    toast({
                      variant: "destructive",
                      title: "Failed to save parameter",
                      description: "Source parameter key is required",
                    });
                    return;
                  }
                  onSave({
                    key,
                    parameterType: "context",
                    sourceParameterKey,
                    description,
                  });
                  return;
                }

                // Handle credential parameters based on type + source combination
                if (type === "credential" || isCredentialSelected) {
                  // Skyvern managed credentials
                  if (credentialSource === "skyvern") {
                    if (!credentialId) {
                      toast({
                        variant: "destructive",
                        title: "Failed to save parameter",
                        description: "Credential is required",
                      });
                      return;
                    }
                    onSave({
                      key,
                      parameterType: "credential",
                      credentialId,
                      description,
                    });
                    return;
                  }

                  // Bitwarden credentials
                  if (credentialSource === "bitwarden") {
                    // Password type
                    if (credentialDataType === "password") {
                      const errorMessage = validateBitwardenLoginCredential(
                        bitwardenCollectionId,
                        bitwardenLoginCredentialItemId,
                        urlParameterKey,
                      );
                      if (errorMessage) {
                        toast({
                          variant: "destructive",
                          title: "Failed to save parameter",
                          description: errorMessage,
                        });
                        return;
                      }
                      onSave({
                        key,
                        parameterType: "credential",
                        itemId:
                          bitwardenLoginCredentialItemId === ""
                            ? null
                            : bitwardenLoginCredentialItemId,
                        urlParameterKey:
                          urlParameterKey === "" ? null : urlParameterKey,
                        collectionId:
                          bitwardenCollectionId === ""
                            ? null
                            : bitwardenCollectionId,
                        description,
                      });
                      return;
                    }

                    // Secret type
                    if (credentialDataType === "secret") {
                      if (!bitwardenCollectionId) {
                        toast({
                          variant: "destructive",
                          title: "Failed to save parameter",
                          description: "Bitwarden Collection ID is required",
                        });
                        return;
                      }
                      onSave({
                        key,
                        parameterType: "secret",
                        collectionId: bitwardenCollectionId,
                        identityFields: identityFields
                          .split(",")
                          .filter((s) => s.length > 0)
                          .map((field) => field.trim()),
                        identityKey,
                        description,
                      });
                      return;
                    }

                    // Credit Card type
                    if (credentialDataType === "creditCard") {
                      if (!bitwardenCollectionId) {
                        toast({
                          variant: "destructive",
                          title: "Failed to save parameter",
                          description: "Bitwarden Collection ID is required",
                        });
                        return;
                      }
                      if (!sensitiveInformationItemId) {
                        toast({
                          variant: "destructive",
                          title: "Failed to save parameter",
                          description: "Bitwarden Item ID is required",
                        });
                        return;
                      }
                      onSave({
                        key,
                        parameterType: "creditCardData",
                        collectionId: bitwardenCollectionId,
                        itemId: sensitiveInformationItemId,
                        description,
                      });
                      return;
                    }
                  }

                  // 1Password credentials
                  if (credentialSource === "onepassword") {
                    if (opVaultId.trim() === "" || opItemId.trim() === "") {
                      toast({
                        variant: "destructive",
                        title: "Failed to save parameter",
                        description:
                          "1Password Vault ID and Item ID are required",
                      });
                      return;
                    }
                    onSave({
                      key,
                      parameterType: "onepassword",
                      vaultId: opVaultId,
                      itemId: opItemId,
                      description,
                    });
                    return;
                  }

                  // Azure Key Vault credentials
                  if (credentialSource === "azurevault") {
                    if (
                      azureVaultName.trim() === "" ||
                      azureUsernameKey.trim() === "" ||
                      azurePasswordKey.trim() === ""
                    ) {
                      toast({
                        variant: "destructive",
                        title: "Failed to add parameter",
                        description:
                          "Azure Vault Name, Username Key and Password Key are required",
                      });
                      return;
                    }
                    onSave({
                      key,
                      parameterType: "credential",
                      vaultName: azureVaultName,
                      usernameKey: azureUsernameKey,
                      passwordKey: azurePasswordKey,
                      totpSecretKey:
                        azureTotpSecretKey === "" ? null : azureTotpSecretKey,
                      description: description,
                    });
                    return;
                  }
                }
              }}
            >
              Save
            </Button>
          </div>
        </div>
      </ScrollAreaViewport>
    </ScrollArea>
  );
}

export { WorkflowParameterEditPanel };
