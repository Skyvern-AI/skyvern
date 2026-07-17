import {
  WorkflowEditorParameterType,
  WorkflowParameterValueType,
} from "../../types/workflowTypes";
import {
  ParametersState,
  parameterIsBitwardenCredential,
  parameterIsAzureVaultCredential,
  parameterIsSkyvernCredential,
} from "../types";

export type ParameterTypeSelection = WorkflowParameterValueType | "credential";

export type CredentialDataType = "password" | "secret" | "creditCard";

export type CredentialSource =
  | "bitwarden"
  | "skyvern"
  | "onepassword"
  | "azurevault"
  | "custom";

export function detectInitialParameterTypeSelection(
  initialValues: ParametersState[number] | undefined,
): ParameterTypeSelection | null {
  if (!initialValues) return null;
  if (initialValues.parameterType === "workflow") return initialValues.dataType;
  if (
    initialValues.parameterType === "credential" ||
    initialValues.parameterType === "secret" ||
    initialValues.parameterType === "creditCardData" ||
    initialValues.parameterType === "onepassword"
  ) {
    return "credential";
  }
  return null;
}

export function detectInitialCredentialDataType(
  initialValues: ParametersState[number] | undefined,
): CredentialDataType {
  if (!initialValues) return "password";
  if (initialValues.parameterType === "secret") return "secret";
  if (initialValues.parameterType === "creditCardData") return "creditCard";
  return "password";
}

export function detectInitialCredentialSource(
  initialValues: ParametersState[number] | undefined,
  skyvernCredentialSourceAvailable: boolean,
): CredentialSource {
  if (!initialValues)
    return skyvernCredentialSourceAvailable ? "skyvern" : "bitwarden";

  if (initialValues.parameterType === "secret") return "bitwarden";
  if (initialValues.parameterType === "creditCardData") return "bitwarden";
  if (initialValues.parameterType === "onepassword") return "onepassword";

  if (initialValues.parameterType === "credential") {
    if (parameterIsSkyvernCredential(initialValues)) return "skyvern";
    if (parameterIsBitwardenCredential(initialValues)) return "bitwarden";
    if (parameterIsAzureVaultCredential(initialValues)) return "azurevault";
  }

  return skyvernCredentialSourceAvailable ? "skyvern" : "bitwarden";
}

export function detectInitialBitwardenManualEntry(
  initialValues: ParametersState[number] | undefined,
): boolean {
  if (!initialValues) return false;

  if (
    initialValues.parameterType === "credential" &&
    parameterIsBitwardenCredential(initialValues)
  ) {
    const itemId = initialValues.itemId ?? "";
    const collectionId = initialValues.collectionId ?? "";
    return (
      !itemId ||
      Boolean(initialValues.urlParameterKey) ||
      itemId.includes("{{") ||
      collectionId.includes("{{")
    );
  }

  if (initialValues.parameterType === "creditCardData") {
    return (
      initialValues.collectionId.includes("{{") ||
      initialValues.itemId.includes("{{")
    );
  }

  return false;
}

export function header(type: WorkflowEditorParameterType, isEdit: boolean) {
  const prefix = isEdit ? "Edit" : "Add";
  if (type === "workflow") {
    return `${prefix} Input`;
  }
  return `${prefix} Context Input`;
}
