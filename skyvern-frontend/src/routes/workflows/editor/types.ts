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

export type CredentialParameterState =
  | BitwardenLoginCredential
  | SkyvernCredential;

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
