import { WorkflowParameterValueType } from "../types/workflowTypes";

export type BitwardenLoginCredential = {
  key: string;
  description?: string | null;
  parameterType: "credential";
  collectionId: string | null;
  itemId: string | null;
  urlParameterKey: string | null;
};

export type SkyvernCredential = {
  key: string;
  description?: string | null;
  parameterType: "credential";
  credentialId: string;
};

export type OnePasswordCredential = {
  key: string;
  description?: string | null;
  parameterType: "onepassword";
  vaultId: string;
  itemId: string;
};

export type AzureVaultCredential = {
  key: string;
  description?: string | null;
  parameterType: "credential";
  vaultName: string;
  usernameKey: string;
  passwordKey: string;
  totpSecretKey: string | null;
};

export function parameterIsBitwardenCredential(
  parameter: CredentialParameterState,
): parameter is BitwardenLoginCredential {
  return "collectionId" in parameter;
}

export function parameterIsSkyvernCredential(
  parameter: CredentialParameterState,
): parameter is SkyvernCredential {
  return "credentialId" in parameter;
}

export function parameterIsOnePasswordCredential(
  parameter: CredentialParameterState,
): parameter is OnePasswordCredential {
  return "vaultId" in parameter && "itemId" in parameter;
}

export function parameterIsAzureVaultCredential(
  parameter: CredentialParameterState,
): parameter is AzureVaultCredential {
  return (
    "vaultName" in parameter &&
    "usernameKey" in parameter &&
    "passwordKey" in parameter
  );
}

export type CredentialParameterState =
  | BitwardenLoginCredential
  | SkyvernCredential
  | OnePasswordCredential
  | AzureVaultCredential;

export type ParametersState = Array<
  | {
      key: string;
      parameterType: "workflow";
      dataType: WorkflowParameterValueType;
      description?: string | null;
      defaultValue: unknown;
    }
  | {
      key: string;
      parameterType: "context";
      sourceParameterKey: string;
      description?: string | null;
    }
  | {
      key: string;
      parameterType: "secret";
      identityKey: string;
      identityFields: Array<string>;
      collectionId: string;
      description?: string | null;
      maybe?: boolean;
    }
  | {
      key: string;
      parameterType: "creditCardData";
      itemId: string;
      collectionId: string;
      description?: string | null;
    }
  | CredentialParameterState
>;
