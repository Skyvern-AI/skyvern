import {
  CredentialFallbackTrigger,
  CredentialParameter,
  WorkflowApiResponse,
  WorkflowParameter,
  WorkflowParameterTypes,
  WorkflowParameterValueType,
} from "./types/workflowTypes";
import { visitWorkflowBlocks } from "./workflowBlockUtils";

export type LoginCredentialInput = {
  parameter: WorkflowParameter | CredentialParameter;
  loginBlockLabels: Array<string>;
  fallbackCredentialIds: Array<string>;
  fallbackTrigger: CredentialFallbackTrigger | null;
};

function isCredentialIdWorkflowParameter(
  parameter: unknown,
): parameter is WorkflowParameter {
  if (parameter === null || typeof parameter !== "object") {
    return false;
  }

  const maybeParameter = parameter as Partial<WorkflowParameter>;
  return (
    maybeParameter.parameter_type === WorkflowParameterTypes.Workflow &&
    maybeParameter.workflow_parameter_type ===
      WorkflowParameterValueType.CredentialId &&
    typeof maybeParameter.key === "string"
  );
}

export function getRotatingCredentialIds(
  parameter: CredentialParameter,
): Array<string> {
  return (parameter.credential_ids ?? []).filter(
    (credentialId, index, allCredentialIds): credentialId is string =>
      typeof credentialId === "string" &&
      credentialId.length > 0 &&
      allCredentialIds.indexOf(credentialId) === index,
  );
}

export function getFallbackCredentialIds(
  parameter: CredentialParameter,
): Array<string> {
  return (parameter.fallback_credential_ids ?? []).filter(
    (credentialId, index, allCredentialIds): credentialId is string =>
      typeof credentialId === "string" &&
      credentialId.length > 0 &&
      allCredentialIds.indexOf(credentialId) === index,
  );
}

function isConfigurableCredentialParameter(
  parameter: unknown,
): parameter is CredentialParameter {
  if (parameter === null || typeof parameter !== "object") {
    return false;
  }

  const maybeParameter = parameter as Partial<CredentialParameter>;
  if (
    maybeParameter.parameter_type !== WorkflowParameterTypes.Credential ||
    typeof maybeParameter.key !== "string"
  ) {
    return false;
  }

  const credentialParameter = maybeParameter as CredentialParameter;
  return (
    getRotatingCredentialIds(credentialParameter).length > 1 ||
    getFallbackCredentialIds(credentialParameter).length > 0
  );
}

function collectLoginCredentialLabels(
  blocks: WorkflowApiResponse["workflow_definition"]["blocks"],
  credentialParametersByKey: Map<
    string,
    WorkflowParameter | CredentialParameter
  >,
  labelsByKey: Map<string, Set<string>>,
  keyOrder: Array<string>,
) {
  visitWorkflowBlocks(blocks, (block) => {
    if (block.block_type === "login") {
      for (const parameter of block.parameters ?? []) {
        if (
          !isCredentialIdWorkflowParameter(parameter) &&
          !isConfigurableCredentialParameter(parameter)
        ) {
          continue;
        }

        if (!credentialParametersByKey.has(parameter.key)) {
          continue;
        }

        if (!labelsByKey.has(parameter.key)) {
          labelsByKey.set(parameter.key, new Set());
          keyOrder.push(parameter.key);
        }
        labelsByKey.get(parameter.key)?.add(block.label);
      }
    }
  });
}

export function getLoginCredentialInputs({
  workflow,
  workflowParameters,
}: {
  workflow: WorkflowApiResponse | undefined;
  workflowParameters: Array<WorkflowParameter>;
}): Array<LoginCredentialInput> {
  if (!workflow) {
    return [];
  }

  const credentialParametersByKey = new Map(
    [
      ...workflowParameters.filter(isCredentialIdWorkflowParameter),
      ...workflow.workflow_definition.parameters.filter(
        isConfigurableCredentialParameter,
      ),
    ].map((parameter) => [parameter.key, parameter]),
  );
  const labelsByKey = new Map<string, Set<string>>();
  const keyOrder: Array<string> = [];

  collectLoginCredentialLabels(
    workflow.workflow_definition.blocks,
    credentialParametersByKey,
    labelsByKey,
    keyOrder,
  );

  return keyOrder.flatMap((key) => {
    const parameter = credentialParametersByKey.get(key);
    const labels = labelsByKey.get(key);
    if (!parameter || !labels) {
      return [];
    }

    const fallbackCredentialIds =
      parameter.parameter_type === WorkflowParameterTypes.Credential
        ? getFallbackCredentialIds(parameter)
        : [];
    const fallbackTrigger =
      fallbackCredentialIds.length > 0 &&
      parameter.parameter_type === WorkflowParameterTypes.Credential
        ? (parameter.fallback_trigger ?? "credential_failures")
        : null;

    return [
      {
        parameter,
        loginBlockLabels: Array.from(labels),
        fallbackCredentialIds,
        fallbackTrigger,
      },
    ];
  });
}
