import { useMemo } from "react";
import { ParametersDialogBase } from "./ParametersDialogBase";
import {
  WorkflowApiResponse,
  WorkflowParameter,
  WorkflowParameterTypes,
  Parameter,
  CredentialParameter,
  AWSSecretParameter,
  OnePasswordCredentialParameter,
  AzureVaultCredentialParameter,
  BitwardenLoginCredentialParameter,
  BitwardenSensitiveInformationParameter,
  BitwardenCreditCardDataParameter,
  ContextParameter,
} from "../types/workflowTypes";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  workflowId: string | null;
  workflows: Array<WorkflowApiResponse>;
};

function getParameterId(param: Parameter): string {
  if ("workflow_parameter_id" in param && param.workflow_parameter_id)
    return param.workflow_parameter_id;
  if ("credential_parameter_id" in param && param.credential_parameter_id)
    return param.credential_parameter_id;
  if ("aws_secret_parameter_id" in param && param.aws_secret_parameter_id)
    return param.aws_secret_parameter_id;
  if (
    "onepassword_credential_parameter_id" in param &&
    param.onepassword_credential_parameter_id
  )
    return param.onepassword_credential_parameter_id;
  if (
    "azure_vault_credential_parameter_id" in param &&
    param.azure_vault_credential_parameter_id
  )
    return param.azure_vault_credential_parameter_id;
  if (
    "bitwarden_login_credential_parameter_id" in param &&
    param.bitwarden_login_credential_parameter_id
  )
    return param.bitwarden_login_credential_parameter_id;
  if (
    "bitwarden_sensitive_information_parameter_id" in param &&
    param.bitwarden_sensitive_information_parameter_id
  )
    return param.bitwarden_sensitive_information_parameter_id;
  if (
    "bitwarden_credit_card_data_parameter_id" in param &&
    param.bitwarden_credit_card_data_parameter_id
  )
    return param.bitwarden_credit_card_data_parameter_id;
  if ("output_parameter_id" in param && param.output_parameter_id)
    return param.output_parameter_id;
  return param.key;
}

function getParameterDisplayType(param: Parameter): string {
  return param.parameter_type;
}

function getParameterDisplayValue(param: Parameter): string | null {
  switch (param.parameter_type) {
    case "workflow": {
      const p = param as WorkflowParameter;
      const value = p.default_value;
      try {
        return value === null || value === undefined
          ? ""
          : typeof value === "string"
            ? value
            : JSON.stringify(value);
      } catch {
        return String(value);
      }
    }
    case "credential": {
      // Show referenced credential id; do not reveal secrets
      return "credential_id" in param
        ? String((param as CredentialParameter).credential_id)
        : null;
    }
    case "aws_secret": {
      // Show the AWS secret key reference only
      return "aws_key" in param
        ? String((param as AWSSecretParameter).aws_key)
        : null;
    }
    case "onepassword": {
      const p = param as OnePasswordCredentialParameter;
      if (p.vault_id && p.item_id) return `${p.vault_id} / ${p.item_id}`;
      return null;
    }
    case "azure_vault_credential": {
      const p = param as AzureVaultCredentialParameter;
      return p.vault_name ? `${p.vault_name}` : null;
    }
    case "bitwarden_login_credential": {
      const p = param as BitwardenLoginCredentialParameter;
      return p.bitwarden_item_id ?? p.bitwarden_collection_id ?? null;
    }
    case "bitwarden_sensitive_information": {
      const p = param as BitwardenSensitiveInformationParameter;
      return p.bitwarden_identity_key ?? null;
    }
    case "bitwarden_credit_card_data": {
      const p = param as BitwardenCreditCardDataParameter;
      return p.bitwarden_item_id ?? null;
    }
    case "context": {
      const p = param as ContextParameter;
      if ("value" in p && p.value !== undefined) {
        try {
          return typeof p.value === "string"
            ? p.value
            : JSON.stringify(p.value);
        } catch {
          return String(p.value);
        }
      }
      return null;
    }
    default:
      return null;
  }
}

// Row rendering moved inside component to access local reveal state

export function WorkflowParametersDialog({
  open,
  onOpenChange,
  workflowId,
  workflows,
}: Props) {
  const workflow = useMemo(
    () => workflows?.find((w) => w.workflow_permanent_id === workflowId),
    [workflows, workflowId],
  );

  const items = useMemo(() => {
    const params = workflow
      ? (workflow.workflow_definition.parameters.filter(
          (p) =>
            p.parameter_type === WorkflowParameterTypes.Workflow ||
            p.parameter_type === "credential" ||
            p.parameter_type === "aws_secret" ||
            p.parameter_type === "onepassword" ||
            p.parameter_type === "azure_vault_credential" ||
            p.parameter_type === "bitwarden_login_credential" ||
            p.parameter_type === "bitwarden_sensitive_information" ||
            p.parameter_type === "bitwarden_credit_card_data" ||
            p.parameter_type === "context",
        ) as Parameter[])
      : ([] as Parameter[]);
    return params.map((param) => ({
      id: getParameterId(param),
      key: param.key,
      description:
        "description" in param ? param.description ?? undefined : undefined,
      type: getParameterDisplayType(param),
      value: getParameterDisplayValue(param),
    }));
  }, [workflow]);

  return (
    <ParametersDialogBase
      open={open}
      onOpenChange={onOpenChange}
      title="Parameters"
      sectionLabel="Workflow-level parameters"
      items={items}
    />
  );
}
