import { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";
import { WorkflowRunStatusApiResponse } from "@/api/types";
import {
  isDisplayedInWorkflowEditor,
  WorkflowEditorParameterTypes,
  WorkflowParameterTypes,
  WorkflowParameterValueType,
} from "../types/workflowTypes";
import { ParametersState } from "./types";

const getInitialParameters = (workflow: WorkflowApiResponse) => {
  return workflow.workflow_definition.parameters
    .filter((parameter) => isDisplayedInWorkflowEditor(parameter))
    .map((parameter) => {
      if (parameter.parameter_type === WorkflowParameterTypes.Workflow) {
        if (
          parameter.workflow_parameter_type ===
          WorkflowParameterValueType.CredentialId
        ) {
          return {
            key: parameter.key,
            parameterType: WorkflowEditorParameterTypes.Credential,
            credentialId: parameter.default_value as string,
            description: parameter.description,
          };
        }
        return {
          key: parameter.key,
          parameterType: WorkflowEditorParameterTypes.Workflow,
          dataType: parameter.workflow_parameter_type,
          defaultValue: parameter.default_value,
          description: parameter.description,
        };
      } else if (parameter.parameter_type === WorkflowParameterTypes.Context) {
        return {
          key: parameter.key,
          parameterType: WorkflowEditorParameterTypes.Context,
          sourceParameterKey: parameter.source.key,
          description: parameter.description,
        };
      } else if (
        parameter.parameter_type ===
        WorkflowParameterTypes.Bitwarden_Sensitive_Information
      ) {
        return {
          key: parameter.key,
          parameterType: WorkflowEditorParameterTypes.Secret,
          collectionId: parameter.bitwarden_collection_id,
          identityKey: parameter.bitwarden_identity_key,
          identityFields: parameter.bitwarden_identity_fields,
          description: parameter.description,
        };
      } else if (
        parameter.parameter_type ===
        WorkflowParameterTypes.Bitwarden_Credit_Card_Data
      ) {
        return {
          key: parameter.key,
          parameterType: WorkflowEditorParameterTypes.CreditCardData,
          collectionId: parameter.bitwarden_collection_id,
          itemId: parameter.bitwarden_item_id,
          description: parameter.description,
        };
      } else if (
        parameter.parameter_type === WorkflowParameterTypes.Credential
      ) {
        return {
          key: parameter.key,
          parameterType: WorkflowEditorParameterTypes.Credential,
          credentialId: parameter.credential_id,
          description: parameter.description,
        };
      } else if (
        parameter.parameter_type === WorkflowParameterTypes.OnePassword
      ) {
        return {
          key: parameter.key,
          parameterType: WorkflowEditorParameterTypes.OnePassword,
          vaultId: parameter.vault_id,
          itemId: parameter.item_id,
          description: parameter.description,
        };
      } else if (
        parameter.parameter_type ===
        WorkflowParameterTypes.Azure_Vault_Credential
      ) {
        return {
          key: parameter.key,
          parameterType: WorkflowEditorParameterTypes.Credential,
          vaultName: parameter.vault_name,
          usernameKey: parameter.username_key,
          passwordKey: parameter.password_key,
          totpSecretKey: parameter.totp_secret_key,
          description: parameter.description,
        };
      } else if (
        parameter.parameter_type ===
        WorkflowParameterTypes.Bitwarden_Login_Credential
      ) {
        return {
          key: parameter.key,
          parameterType: WorkflowEditorParameterTypes.Credential,
          collectionId: parameter.bitwarden_collection_id,
          itemId: parameter.bitwarden_item_id,
          urlParameterKey: parameter.url_parameter_key,
          description: parameter.description,
        };
      }
      return undefined;
    })
    .filter(Boolean) as ParametersState;
};

/**
 * Attempt to construct a valid code key value from the workflow parameters.
 */
const constructCacheKeyValue = (opts: {
  codeKey: string;
  workflow?: WorkflowApiResponse;
  workflowRun?: WorkflowRunStatusApiResponse;
}) => {
  const { workflow, workflowRun } = opts;
  const codeKey = opts.codeKey;

  if (!workflow) {
    return "";
  }

  const workflowParameters = workflowRun
    ? workflowRun?.parameters ?? {}
    : getInitialParameters(workflow)
        .filter((p) => p.parameterType === "workflow")
        .reduce(
          (acc, parameter) => {
            acc[parameter.key] = parameter.defaultValue;
            return acc;
          },
          {} as Record<string, unknown>,
        );

  return constructCacheKeyValueFromParameters({
    codeKey,
    parameters: workflowParameters,
  });
};

const constructCacheKeyValueFromParameters = (opts: {
  codeKey: string;
  parameters: Record<string, unknown>;
}) => {
  const parameters = opts.parameters;
  let codeKey = opts.codeKey;

  for (const [name, value] of Object.entries(parameters)) {
    if (value === null || value === undefined || value === "") {
      continue;
    }

    codeKey = codeKey.replace(`{{${name}}}`, value.toString());
  }

  if (codeKey.includes("{") || codeKey.includes("}")) {
    return "";
  }

  return codeKey;
};

export {
  constructCacheKeyValue,
  constructCacheKeyValueFromParameters,
  getInitialParameters,
};
