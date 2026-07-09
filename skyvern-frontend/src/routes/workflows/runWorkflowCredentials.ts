import {
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

function isRotatingCredentialParameter(
  parameter: unknown,
): parameter is CredentialParameter {
  if (parameter === null || typeof parameter !== "object") {
    return false;
  }

  const maybeParameter = parameter as Partial<CredentialParameter>;
  return (
    maybeParameter.parameter_type === WorkflowParameterTypes.Credential &&
    typeof maybeParameter.key === "string" &&
    getRotatingCredentialIds(maybeParameter as CredentialParameter).length > 1
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
          !isRotatingCredentialParameter(parameter)
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
        isRotatingCredentialParameter,
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

    return [
      {
        parameter,
        loginBlockLabels: Array.from(labels),
      },
    ];
  });
}
