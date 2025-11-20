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
  { label: "float", value: WorkflowParameterValueType.Float },
  { label: "integer", value: WorkflowParameterValueType.Integer },
  { label: "boolean", value: WorkflowParameterValueType.Boolean },
  { label: "file", value: WorkflowParameterValueType.FileURL },
  { label: "credential", value: WorkflowParameterValueType.CredentialId },
  { label: "JSON", value: WorkflowParameterValueType.JSON },
];

function header(type: WorkflowEditorParameterType, isEdit: boolean) {
  const prefix = isEdit ? "Edit" : "Add";
  if (type === "workflow") {
    return `${prefix} Input Parameter`;
  }
  if (type === "credential") {
    return `${prefix} Credential Parameter`;
  }
  if (type === "secret") {
    return `${prefix} Secret Parameter`;
  }
  if (type === "creditCardData") {
    return `${prefix} Credit Card Parameter`;
  }
  return `${prefix} Context Parameter`;
}

function WorkflowParameterEditPanel({
  type,
  onClose,
  onSave,
  initialValues,
}: Props) {
  const reservedKeys = ["current_item", "current_value", "current_index"];
  const isCloud = useContext(CloudContext);
  const isEditMode = !!initialValues;
  const [key, setKey] = useState(initialValues?.key ?? "");
  const hasWhitespace = /\s/.test(key);
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
  const [credentialType, setCredentialType] = useState<
    "bitwarden" | "skyvern" | "onepassword" | "azurevault"
  >(
    isBitwardenCredential
      ? "bitwarden"
      : isOnePasswordCredential
        ? "onepassword"
        : isAzureVaultCredential
          ? "azurevault"
          : "skyvern",
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
  const [parameterType, setParameterType] =
    useState<WorkflowParameterValueType>(
      initialValues?.parameterType === "workflow"
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

  return (
    <ScrollArea>
      <ScrollAreaViewport className="max-h-[500px]">
        <div className="space-y-4 p-1 px-4">
          <header className="flex items-center justify-between">
            <span>{header(type, isEditMode)}</span>
            <Cross2Icon className="h-6 w-6 cursor-pointer" onClick={onClose} />
          </header>
          <div className="space-y-1">
            <Label className="text-xs text-slate-300">Key</Label>
            <Input value={key} onChange={(e) => setKey(e.target.value)} />
            {hasWhitespace && (
              <p className="text-xs text-destructive">
                Spaces are not allowed, consider using _
              </p>
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
                    setParameterType(value as WorkflowParameterValueType);
                    setDefaultValueState((state) => {
                      return {
                        ...state,
                        defaultValue: getDefaultValueForParameterType(
                          value as WorkflowParameterValueType,
                        ),
                      };
                    });
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
              </div>
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
                        defaultValue:
                          getDefaultValueForParameterType(parameterType),
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
                    type={parameterType}
                    value={defaultValueState.defaultValue}
                  />
                )}
              </div>
            </>
          )}
          {type === "credential" && (
            <>
              <div className="space-y-1">
                <Label className="text-xs text-slate-300">
                  Credential Type
                </Label>
                <Select
                  value={credentialType}
                  onValueChange={(value) => {
                    setCredentialType(
                      value as
                        | "bitwarden"
                        | "skyvern"
                        | "onepassword"
                        | "azurevault",
                    );
                  }}
                >
                  <SelectTrigger className="w-full">
                    <SelectValue placeholder="Select a type" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      <SelectItem value="skyvern">Skyvern</SelectItem>
                      <SelectItem value="bitwarden">Bitwarden</SelectItem>
                      <SelectItem value="onepassword">1Password</SelectItem>
                      <SelectItem value="azurevault">Azure Vault</SelectItem>
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </div>
            </>
          )}
          {type === "credential" && credentialType === "bitwarden" && (
            <>
              <div className="space-y-1">
                <Label className="text-xs text-slate-300">
                  URL Parameter Key
                </Label>
                <Input
                  value={urlParameterKey}
                  onChange={(e) => setUrlParameterKey(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs text-slate-300">Collection ID</Label>
                <Input
                  value={bitwardenCollectionId}
                  onChange={(e) => setBitwardenCollectionId(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs text-slate-300">Item ID</Label>
                <Input
                  value={bitwardenLoginCredentialItemId}
                  onChange={(e) =>
                    setBitwardenLoginCredentialItemId(e.target.value)
                  }
                />
              </div>
            </>
          )}
          {type === "credential" && credentialType === "onepassword" && (
            <>
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">Vault ID</Label>
                  <HelpTooltip content="You can find the Vault ID and Item ID in the URL when viewing the item in 1Password on the web." />
                </div>
                <Input
                  value={opVaultId}
                  onChange={(e) => setOpVaultId(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">Item ID</Label>
                  <HelpTooltip content="Supports all 1Password item types: Logins, Passwords, Credit Cards, Secure Notes, and more." />
                </div>
                <Input
                  value={opItemId}
                  onChange={(e) => setOpItemId(e.target.value)}
                />
              </div>
              <div className="rounded-md bg-slate-800 p-2">
                <div className="space-y-1 text-xs text-slate-400">
                  Credit Cards: Due to a 1Password limitation, add the
                  expiration date as a separate text field named “Expire Date”
                  in the format MM/YYYY (e.g. 09/2027).
                </div>
              </div>
            </>
          )}
          {type === "credential" && credentialType === "azurevault" && (
            <>
              <div className="space-y-1">
                <Label className="text-xs text-slate-300">Vault Name</Label>
                <Input
                  value={azureVaultName}
                  onChange={(e) => setAzureVaultName(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs text-slate-300">Username Key</Label>
                <Input
                  autoComplete="off"
                  value={azureUsernameKey}
                  onChange={(e) => setAzureUsernameKey(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs text-slate-300">Password Key</Label>
                <Input
                  value={azurePasswordKey}
                  onChange={(e) => setAzurePasswordKey(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs text-slate-300">
                  TOTP Secret Key
                </Label>
                <Input
                  value={azureTotpSecretKey}
                  onChange={(e) => setAzureTotpKey(e.target.value)}
                />
              </div>
            </>
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
          {type === "secret" && (
            <>
              <div className="space-y-1">
                <Label className="text-xs text-slate-300">Identity Key</Label>
                <Input
                  value={identityKey}
                  onChange={(e) => setIdentityKey(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs text-slate-300">
                  Identity Fields
                </Label>
                <Input
                  value={identityFields}
                  onChange={(e) => setIdentityFields(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs text-slate-300">Collection ID</Label>
                <Input
                  value={bitwardenCollectionId}
                  onChange={(e) => setBitwardenCollectionId(e.target.value)}
                />
              </div>
            </>
          )}
          {type === "creditCardData" && (
            <>
              <div className="space-y-1">
                <Label className="text-xs text-slate-300">Collection ID</Label>
                <Input
                  value={bitwardenCollectionId}
                  onChange={(e) => setBitwardenCollectionId(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs text-slate-300">Item ID</Label>
                <Input
                  value={sensitiveInformationItemId}
                  onChange={(e) =>
                    setSensitiveInformationItemId(e.target.value)
                  }
                />
              </div>
            </>
          )}
          {
            // temporarily cloud only
            type === "credential" &&
              credentialType === "skyvern" &&
              isCloud && (
                <div className="space-y-1">
                  <Label className="text-xs text-slate-300">Credential</Label>
                  <CredentialParameterSourceSelector
                    value={credentialId}
                    onChange={(value) => setCredentialId(value)}
                  />
                </div>
              )
          }
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
                if (hasWhitespace) {
                  toast({
                    variant: "destructive",
                    title: "Failed to save parameter",
                    description:
                      "Key cannot contain whitespace characters. Consider using underscores (_) instead.",
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
                if (type === "workflow") {
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
                    dataType: parameterType,
                    description,
                    defaultValue: defaultValueState.hasDefaultValue
                      ? defaultValue
                      : null,
                  });
                }
                if (type === "credential" && credentialType === "bitwarden") {
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
                }
                if (type === "credential" && credentialType === "onepassword") {
                  if (opVaultId.trim() === "" || opItemId.trim() === "") {
                    toast({
                      variant: "destructive",
                      title: "Failed to save parameter",
                      description: "Vault ID and Item ID are required",
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
                }
                if (type === "credential" && credentialType === "azurevault") {
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
                }
                if (type === "secret" || type === "creditCardData") {
                  if (!bitwardenCollectionId) {
                    toast({
                      variant: "destructive",
                      title: "Failed to save parameter",
                      description: "Collection ID is required",
                    });
                    return;
                  }
                }
                if (type === "secret") {
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
                }
                if (type === "creditCardData") {
                  onSave({
                    key,
                    parameterType: "creditCardData",
                    collectionId: bitwardenCollectionId,
                    itemId: sensitiveInformationItemId,
                    description,
                  });
                }
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
                }
                if (type === "credential" && credentialType === "skyvern") {
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
