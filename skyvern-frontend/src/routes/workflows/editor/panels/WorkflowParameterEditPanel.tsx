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
import { cn } from "@/util/utils";
import { CodeIcon, Cross2Icon } from "@radix-ui/react-icons";
import { useEffect, useRef, useState } from "react";
import { BitwardenItemSelector } from "../../components/BitwardenItemSelector";
import { CredentialParameterSourceSelector } from "../../components/CredentialParameterSourceSelector";
import { OnePasswordItemSelector } from "../../components/OnePasswordItemSelector";
import { SourceParameterKeySelector } from "../../components/SourceParameterKeySelector";
import { useOnePasswordItemsQuery } from "../../hooks/useOnePasswordItemsQuery";
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
  AUTO_GENERATED_CREDENTIAL_KEY_PATTERN,
} from "../types";
import { getDefaultValueForParameterType } from "../workflowEditorUtils";
import { validateBitwardenLoginCredential } from "./util";
import { HelpTooltip } from "@/components/HelpTooltip";
import { useCustomCredentialServiceConfig } from "@/hooks/useCustomCredentialServiceConfig";
import { getInvalidJsonMessage } from "@/util/jsonParseError";
import {
  CredentialDataType,
  CredentialSource,
  detectInitialBitwardenManualEntry,
  detectInitialCredentialDataType,
  detectInitialCredentialSource,
  detectInitialParameterTypeSelection,
  header,
  ParameterTypeSelection,
} from "./WorkflowParameterEditPanel.helpers";
import { useSkyvernCredentialSourceAvailable } from "../../hooks/useSkyvernCredentialSourceAvailable";

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

// Determine available sources based on credential data type
function getAvailableSourcesForDataType(
  dataType: CredentialDataType,
  skyvernCredentialSourceAvailable: boolean,
  hasCustomCredentialService: boolean,
): Array<{ value: CredentialSource; label: string }> {
  const customOption = hasCustomCredentialService
    ? [
        {
          value: "custom" as const,
          label: "Custom Credential Service",
        },
      ]
    : [];

  switch (dataType) {
    case "password":
      return [
        ...(skyvernCredentialSourceAvailable
          ? [{ value: "skyvern" as const, label: "Skyvern" }]
          : []),
        { value: "bitwarden" as const, label: "Bitwarden" },
        { value: "onepassword" as const, label: "1Password" },
        { value: "azurevault" as const, label: "Azure Key Vault" },
        ...customOption,
      ];
    case "secret":
      return [
        ...(skyvernCredentialSourceAvailable
          ? [{ value: "skyvern" as const, label: "Skyvern" }]
          : []),
        { value: "bitwarden" as const, label: "Bitwarden" },
        ...customOption,
      ];
    case "creditCard":
      return [
        ...(skyvernCredentialSourceAvailable
          ? [{ value: "skyvern" as const, label: "Skyvern" }]
          : []),
        { value: "bitwarden" as const, label: "Bitwarden" },
        { value: "onepassword" as const, label: "1Password" },
        ...customOption,
      ];
  }
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
      return "Key cannot start with a digit. Input keys must start with a letter or underscore.";
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

function BitwardenItemFieldHeader({
  manualEntry,
  onToggle,
  tooltip,
}: {
  manualEntry: boolean;
  onToggle: () => void;
  tooltip: string;
}) {
  return (
    <div className="flex items-center justify-between gap-2">
      <div className="flex items-center gap-2">
        <Label className="text-xs text-tertiary-foreground">
          Bitwarden Item
        </Label>
        <HelpTooltip content={tooltip} />
      </div>
      <button
        type="button"
        aria-pressed={manualEntry}
        title={
          manualEntry
            ? "Pick from your Bitwarden items"
            : "Enter a custom value"
        }
        className={cn(
          "rounded p-1 text-muted-foreground transition-colors hover:text-foreground dark:hover:text-slate-200",
          manualEntry && "bg-muted text-foreground dark:bg-slate-700",
        )}
        onClick={onToggle}
      >
        <CodeIcon className="size-4" />
      </button>
    </div>
  );
}

function BitwardenManualInput({
  label,
  onChange,
  tooltip,
  value,
}: {
  label: string;
  onChange: (value: string) => void;
  tooltip: string;
  value: string;
}) {
  return (
    <div className="space-y-1">
      <div className="flex gap-2">
        <Label className="text-xs text-tertiary-foreground">{label}</Label>
        <HelpTooltip content={tooltip} />
      </div>
      <Input value={value} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
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
  const skyvernCredentialSourceAvailable =
    useSkyvernCredentialSourceAvailable();
  const { parsedConfig: customCredentialServiceConfig } =
    useCustomCredentialServiceConfig();
  const hasCustomCredentialService = customCredentialServiceConfig !== null;
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
    detectInitialCredentialSource(
      initialValues,
      skyvernCredentialSourceAvailable,
    ),
  );
  const credentialSourceUserChangedRef = useRef(false);
  const previousSkyvernCredentialSourceAvailableRef = useRef(
    skyvernCredentialSourceAvailable,
  );

  useEffect(() => {
    const becameAvailable =
      !previousSkyvernCredentialSourceAvailableRef.current &&
      skyvernCredentialSourceAvailable;
    previousSkyvernCredentialSourceAvailableRef.current =
      skyvernCredentialSourceAvailable;

    if (
      becameAvailable &&
      !initialValues &&
      !credentialSourceUserChangedRef.current
    ) {
      setCredentialSource("skyvern");
    }
  }, [initialValues, skyvernCredentialSourceAvailable]);

  const [urlParameterKey, setUrlParameterKey] = useState(
    isBitwardenCredential ? (initialValues?.urlParameterKey ?? "") : "",
  );
  const [description, setDescription] = useState(
    initialValues?.description ?? "",
  );
  const [bitwardenCollectionId, setBitwardenCollectionId] = useState(
    isBitwardenCredential ||
      initialValues?.parameterType === "secret" ||
      initialValues?.parameterType === "creditCardData"
      ? (initialValues?.collectionId ?? "")
      : "",
  );
  const initialParameterTypeSelection =
    detectInitialParameterTypeSelection(initialValues);
  const [parameterType, setParameterType] = useState<ParameterTypeSelection>(
    initialParameterTypeSelection ?? "string",
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
  const [opManualEntry, setOpManualEntry] = useState(
    isOnePasswordCredential &&
      (initialValues.vaultId.includes("{{") ||
        initialValues.itemId.includes("{{")),
  );
  const opEditModeInitializedRef = useRef(false);
  const opUserTouchedRef = useRef(false);
  const savedOpVaultId = isOnePasswordCredential ? initialValues.vaultId : "";
  const savedOpItemId = isOnePasswordCredential ? initialValues.itemId : "";

  const [bitwardenLoginCredentialItemId, setBitwardenLoginCredentialItemId] =
    useState(isBitwardenCredential ? (initialValues?.itemId ?? "") : "");
  const [bitwardenManualEntry, setBitwardenManualEntry] = useState(
    detectInitialBitwardenManualEntry(initialValues),
  );

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
    isAzureVaultCredential ? (initialValues.totpSecretKey ?? "") : "",
  );

  // Handle credential data type change - reset source to first available
  const handleCredentialDataTypeChange = (newDataType: CredentialDataType) => {
    setCredentialDataType(newDataType);
    setOpVaultId("");
    setOpItemId("");
    setBitwardenLoginCredentialItemId("");
    setBitwardenCollectionId("");
    setUrlParameterKey("");
    setSensitiveInformationItemId("");
    setBitwardenManualEntry(false);
    const availableSources = getAvailableSourcesForDataType(
      newDataType,
      skyvernCredentialSourceAvailable,
      hasCustomCredentialService,
    );
    if (!availableSources.find((s) => s.value === credentialSource)) {
      setCredentialSource(availableSources[0]?.value ?? "bitwarden");
    }
  };

  const availableSources = getAvailableSourcesForDataType(
    credentialDataType,
    skyvernCredentialSourceAvailable,
    hasCustomCredentialService,
  );

  const isCredentialSelected = parameterType === "credential";
  const showCredentialFields = type === "workflow" && isCredentialSelected;

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
    showCredentialFields &&
    credentialSource === "skyvern" &&
    skyvernCredentialSourceAvailable;
  const showCustomCredentialSelector =
    showCredentialFields &&
    credentialSource === "custom" &&
    hasCustomCredentialService;
  const onePasswordItemsQuery = useOnePasswordItemsQuery({
    enabled: showOnePasswordFields,
  });

  useEffect(() => {
    if (
      !showOnePasswordFields ||
      !isOnePasswordCredential ||
      opEditModeInitializedRef.current ||
      opUserTouchedRef.current
    ) {
      return;
    }

    // Keep the saved vault/item IDs visible (manual mode) when the item list can't load.
    if (onePasswordItemsQuery.isError) {
      opEditModeInitializedRef.current = true;
      setOpManualEntry(true);
      return;
    }

    if (!onePasswordItemsQuery.data) {
      return;
    }

    opEditModeInitializedRef.current = true;
    const savedItemExists = onePasswordItemsQuery.data.items.some(
      (item) =>
        item.vault_id === savedOpVaultId && item.item_id === savedOpItemId,
    );
    setOpManualEntry(!savedItemExists);
  }, [
    isOnePasswordCredential,
    onePasswordItemsQuery.data,
    onePasswordItemsQuery.isError,
    savedOpItemId,
    savedOpVaultId,
    showOnePasswordFields,
  ]);

  return (
    <ScrollArea>
      <ScrollAreaViewport className="max-h-[calc(100vh-8rem)]">
        <div className="space-y-4 p-1 px-4">
          <header className="flex items-center justify-between">
            <span>{header(type, isEditMode)}</span>
            <Cross2Icon className="h-6 w-6 cursor-pointer" onClick={onClose} />
          </header>
          <div className="space-y-1">
            <Label className="text-xs text-tertiary-foreground">Key</Label>
            <Input value={key} onChange={(e) => setKey(e.target.value)} />
            {keyValidationError && (
              <p className="text-xs text-destructive">{keyValidationError}</p>
            )}
          </div>
          <div className="space-y-1">
            <Label className="text-xs text-tertiary-foreground">
              Description
            </Label>
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
                      credentialSourceUserChangedRef.current = false;
                      setCredentialId("");
                      setCredentialDataType("password");
                      setCredentialSource(
                        skyvernCredentialSourceAvailable
                          ? "skyvern"
                          : "bitwarden",
                      );
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
                      {workflowParameterTypeOptions.map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
                {isEditMode &&
                  initialParameterTypeSelection !== null &&
                  initialParameterTypeSelection !== parameterType && (
                    <p className="text-xs text-amber-700 dark:text-amber-400">
                      Changing the type of an existing parameter may break
                      blocks that reference it.
                    </p>
                  )}
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
                    <Label className="text-xs text-tertiary-foreground">
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
                  <Label className="text-xs text-tertiary-foreground">
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
                  <Label className="text-xs text-tertiary-foreground">
                    Source
                  </Label>
                  <HelpTooltip content="Select the storage location for your credentials. Skyvern supports managed credentials such as Bitwarden, 1Password, and Azure Key Vault that connect directly to your vault. If you use a custom external credential service, you can add it here as well." />
                </div>
                <Select
                  value={credentialSource}
                  onValueChange={(value) => {
                    credentialSourceUserChangedRef.current = true;
                    setCredentialSource(value as CredentialSource);
                  }}
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
            <div className="space-y-1">
              <BitwardenItemFieldHeader
                manualEntry={bitwardenManualEntry}
                tooltip="Pick an item from your connected Bitwarden account. Click the </> button to pass in a custom value instead, for example a dynamic {{ input }} reference or raw Bitwarden IDs."
                onToggle={() => {
                  if (bitwardenManualEntry) {
                    if (bitwardenLoginCredentialItemId.includes("{{")) {
                      setBitwardenLoginCredentialItemId("");
                    }
                    setUrlParameterKey("");
                    setBitwardenCollectionId("");
                  }
                  setBitwardenManualEntry((prev) => !prev);
                }}
              />
              {bitwardenManualEntry ? (
                <div className="space-y-3">
                  <BitwardenManualInput
                    label="URL Parameter Key"
                    value={urlParameterKey}
                    onChange={setUrlParameterKey}
                    tooltip="Optional. The agent input key that holds the URL. If provided, Skyvern will match the credential based on this URL."
                  />
                  <BitwardenManualInput
                    label="Bitwarden Collection ID"
                    value={bitwardenCollectionId}
                    onChange={setBitwardenCollectionId}
                    tooltip="Find in the Bitwarden collection URL. Supports agent inputs."
                  />
                  <BitwardenManualInput
                    label="Bitwarden Item ID"
                    value={bitwardenLoginCredentialItemId}
                    onChange={setBitwardenLoginCredentialItemId}
                    tooltip="Find in /#/vault?itemId=[ITEM_ID]. Supports agent inputs."
                  />
                </div>
              ) : (
                <BitwardenItemSelector
                  itemId={bitwardenLoginCredentialItemId}
                  credentialDataType="password"
                  onSelect={(collectionId, itemId) => {
                    setBitwardenLoginCredentialItemId(itemId);
                    setBitwardenCollectionId(collectionId ?? "");
                    setUrlParameterKey("");
                  }}
                />
              )}
            </div>
          )}

          {/* Bitwarden Secret Fields */}
          {showBitwardenSecretFields && (
            <>
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-tertiary-foreground">
                    Bitwarden Collection ID
                  </Label>
                  <HelpTooltip content="Collection containing the identity, such as {{ parameter_name }}." />
                </div>
                <Input
                  value={bitwardenCollectionId}
                  onChange={(e) => setBitwardenCollectionId(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-tertiary-foreground">
                    Identity Key
                  </Label>
                  <HelpTooltip content="Identity name or identifier, such as {{ parameter_name }}." />
                </div>
                <Input
                  value={identityKey}
                  onChange={(e) => setIdentityKey(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-tertiary-foreground">
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
            <div className="space-y-1">
              <BitwardenItemFieldHeader
                manualEntry={bitwardenManualEntry}
                tooltip="Pick a credit card from your connected Bitwarden account. Click the </> button to pass in custom collection/item IDs or dynamic {{ input }} references."
                onToggle={() => {
                  if (
                    bitwardenManualEntry &&
                    (bitwardenCollectionId.includes("{{") ||
                      sensitiveInformationItemId.includes("{{"))
                  ) {
                    setBitwardenCollectionId("");
                    setSensitiveInformationItemId("");
                  }
                  setBitwardenManualEntry((prev) => !prev);
                }}
              />
              {bitwardenManualEntry ? (
                <div className="space-y-3">
                  <BitwardenManualInput
                    label="Bitwarden Collection ID"
                    value={bitwardenCollectionId}
                    onChange={setBitwardenCollectionId}
                    tooltip="Collection containing the credit card. Supports agent inputs."
                  />
                  <BitwardenManualInput
                    label="Bitwarden Item ID"
                    value={sensitiveInformationItemId}
                    onChange={setSensitiveInformationItemId}
                    tooltip="Credit card item ID. Supports agent inputs."
                  />
                </div>
              ) : (
                <BitwardenItemSelector
                  itemId={sensitiveInformationItemId}
                  credentialDataType="creditCard"
                  onSelect={(collectionId, itemId) => {
                    setBitwardenCollectionId(collectionId ?? "");
                    setSensitiveInformationItemId(itemId);
                  }}
                />
              )}
            </div>
          )}

          {/* 1Password Fields */}
          {showOnePasswordFields && (
            <>
              <div className="space-y-1">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <Label className="text-xs text-tertiary-foreground">
                      1Password Item
                    </Label>
                    <HelpTooltip
                      content={
                        "Pick an item from your connected 1Password account. Click the </> button to pass in a custom value instead — for example a dynamic {{ input }} reference or a raw vault/item ID."
                      }
                    />
                  </div>
                  <button
                    type="button"
                    aria-pressed={opManualEntry}
                    title={
                      opManualEntry
                        ? "Pick from your 1Password items"
                        : "Enter a custom value"
                    }
                    className={cn(
                      "rounded p-1 text-muted-foreground transition-colors hover:text-foreground dark:hover:text-slate-200",
                      opManualEntry &&
                        "bg-muted text-foreground dark:bg-slate-700",
                    )}
                    onClick={() => {
                      opUserTouchedRef.current = true;
                      if (opManualEntry) {
                        const items = onePasswordItemsQuery.data?.items ?? [];
                        const matches = items.some(
                          (item) =>
                            item.vault_id === opVaultId &&
                            item.item_id === opItemId,
                        );
                        if (!matches) {
                          setOpVaultId("");
                          setOpItemId("");
                        }
                      }
                      setOpManualEntry((prev) => !prev);
                    }}
                  >
                    <CodeIcon className="size-4" />
                  </button>
                </div>
                {opManualEntry ? (
                  <div className="space-y-3">
                    <div className="space-y-1">
                      <div className="flex gap-2">
                        <Label className="text-xs text-tertiary-foreground">
                          1Password Vault ID
                        </Label>
                        <HelpTooltip content="Find this in the 1Password vault URL. Supports dynamic agent inputs like {{ my_input }}." />
                      </div>
                      <Input
                        value={opVaultId}
                        onChange={(e) => {
                          opUserTouchedRef.current = true;
                          setOpVaultId(e.target.value);
                        }}
                      />
                    </div>
                    <div className="space-y-1">
                      <div className="flex gap-2">
                        <Label className="text-xs text-tertiary-foreground">
                          1Password Item ID
                        </Label>
                        <HelpTooltip content="Find this in the 1Password item URL. Supports dynamic agent inputs like {{ my_input }}." />
                      </div>
                      <Input
                        value={opItemId}
                        onChange={(e) => {
                          opUserTouchedRef.current = true;
                          setOpItemId(e.target.value);
                        }}
                      />
                    </div>
                  </div>
                ) : (
                  <OnePasswordItemSelector
                    vaultId={opVaultId}
                    itemId={opItemId}
                    credentialDataType={credentialDataType}
                    onSelect={(vaultId, itemId) => {
                      opUserTouchedRef.current = true;
                      setOpVaultId(vaultId);
                      setOpItemId(itemId);
                    }}
                  />
                )}
              </div>
              {credentialDataType === "creditCard" && (
                <div className="rounded-md bg-muted p-2">
                  <div className="space-y-1 text-xs text-muted-foreground">
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
                  <Label className="text-xs text-tertiary-foreground">
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
                  <Label className="text-xs text-tertiary-foreground">
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
                  <Label className="text-xs text-tertiary-foreground">
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
                  <Label className="text-xs text-tertiary-foreground">
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
                <Label className="text-xs text-tertiary-foreground">
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

          {/* Custom Credential Service Selector */}
          {showCustomCredentialSelector && (
            <div className="space-y-1">
              <div className="flex gap-2">
                <Label className="text-xs text-tertiary-foreground">
                  Custom Credential
                </Label>
                <HelpTooltip content="Select a credential managed by your custom credential service. These credentials are stored in your external credential vault." />
              </div>
              <CredentialParameterSourceSelector
                value={credentialId}
                onChange={(value) => setCredentialId(value)}
                vault_type="custom"
              />
            </div>
          )}

          {type === "context" && (
            <div className="space-y-1">
              <Label className="text-xs text-tertiary-foreground">
                Source Input
              </Label>
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
                    title: "Failed to save input",
                    description: "Key is required",
                  });
                  return;
                }
                if (keyValidationError) {
                  toast({
                    variant: "destructive",
                    title: "Failed to save input",
                    description: keyValidationError,
                  });
                  return;
                }
                if (!isEditMode && reservedKeys.includes(key)) {
                  toast({
                    variant: "destructive",
                    title: "Failed to add input",
                    description: `${key} is reserved, please use another key`,
                  });
                  return;
                }
                // `credentials`/`credentials_N` keys are reserved for the Login
                // block's auto-generated credential wrappers — letting a user author
                // one would make the auto-gen-vs-user-authored provenance heuristic
                // ambiguous. Block only when the key is newly set to a reserved name.
                if (
                  (type === "credential" || isCredentialSelected) &&
                  key !== initialValues?.key &&
                  AUTO_GENERATED_CREDENTIAL_KEY_PATTERN.test(key)
                ) {
                  toast({
                    variant: "destructive",
                    title: "Failed to save input",
                    description: `"${key}" is reserved for auto-generated credential variables. Please choose a different key.`,
                  });
                  return;
                }

                // Handle workflow parameters (non-credential)
                if (type === "workflow" && !isCredentialSelected) {
                  let defaultValue = defaultValueState.defaultValue;

                  if (
                    parameterType === "json" &&
                    typeof defaultValueState.defaultValue === "string"
                  ) {
                    try {
                      defaultValue = JSON.parse(defaultValueState.defaultValue);
                    } catch (e) {
                      toast({
                        variant: "destructive",
                        title: "Failed to save input",
                        description: getInvalidJsonMessage(
                          defaultValueState.defaultValue,
                          e,
                        ),
                      });
                      return;
                    }
                  }
                  // Convert boolean to string for backend storage
                  if (
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
                      title: "Failed to save input",
                      description: "Source input key is required",
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

                // Handle credential parameters based on source + data-type combination
                if (isCredentialSelected) {
                  // Skyvern managed credentials or Custom credential service
                  if (
                    credentialSource === "skyvern" ||
                    credentialSource === "custom"
                  ) {
                    if (!credentialId) {
                      toast({
                        variant: "destructive",
                        title: "Failed to save input",
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
                          title: "Failed to save input",
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
                          title: "Failed to save input",
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
                          title: "Failed to save input",
                          description: "Bitwarden Collection ID is required",
                        });
                        return;
                      }
                      if (!sensitiveInformationItemId) {
                        toast({
                          variant: "destructive",
                          title: "Failed to save input",
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
                        title: "Failed to save input",
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
                        title: "Failed to add input",
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
