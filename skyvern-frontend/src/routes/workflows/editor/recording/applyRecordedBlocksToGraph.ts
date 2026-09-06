import { Edge } from "@xyflow/react";
import { nanoid } from "nanoid";

import type { WorkflowBlock } from "@/routes/workflows/types/workflowTypes";
import type {
  InsertionPoint,
  RecordedParameter,
} from "@/store/RecordedBlocksStore";
import { AppNode, isWorkflowBlockNode } from "../nodes";
import {
  generateDefaultCredentialParameterKey,
  ParametersState,
} from "../types";
import { convertToNode, generateNodeLabel } from "../workflowEditorUtils";

/**
 * The recorder cannot see the target workflow, so it keys each credential by its id — a
 * token, not a key. Allocation happens here, the one place the live parameter set is known:
 * a parameter that already wraps the same credential is reused, otherwise the next free
 * auto-generated key is taken. Two recorded credentials can never land on one key, and a
 * recorded credential can never shadow a different one the workflow already owns.
 */
function allocateCredentialKeys(
  recordedParameters: Array<RecordedParameter>,
  existingParameters: ParametersState,
): Map<string, string> {
  const keyByToken = new Map<string, string>();
  const takenKeys = new Set(
    existingParameters.map((parameter) => parameter.key),
  );

  for (const parameter of recordedParameters) {
    if (parameter.parameter_type !== "credential") {
      continue;
    }
    const wrapper = existingParameters.find(
      (p) => "credentialId" in p && p.credentialId === parameter.credential_id,
    );
    const key =
      wrapper?.key ?? generateDefaultCredentialParameterKey([...takenKeys]);
    takenKeys.add(key);
    keyByToken.set(parameter.key, key);
  }

  return keyByToken;
}

/**
 * A recorded secret fill carries the token inside its instruction text — the recorder writes
 * `Type 'API token' with {{ cred_123.secret_value }}.` — so the declaration and the reference
 * have to move together. A credential id never occurs in prose otherwise, so the match is
 * exact.
 */
function substituteGoalTokens(
  navigationGoal: string,
  keyByToken: Map<string, string>,
): string {
  let goal = navigationGoal;
  for (const [token, key] of keyByToken) {
    goal = goal.replace(new RegExp(`(\\{\\{\\s*)${token}\\b`, "g"), `$1${key}`);
  }
  return goal;
}

function substituteCredentialTokens(
  block: WorkflowBlock,
  keyByToken: Map<string, string>,
): WorkflowBlock {
  const rename = (key: string) => keyByToken.get(key) ?? key;
  // One structural cast rather than a switch over every block variant: only these three
  // fields are touched, and they carry the same meaning on every variant that has them.
  const withParameters = block as WorkflowBlock & {
    parameters?: Array<{ key: string }>;
    parameter_keys?: Array<string>;
    navigation_goal?: string | null;
  };

  if (
    !withParameters.parameters &&
    !withParameters.parameter_keys &&
    !withParameters.navigation_goal
  ) {
    return block;
  }

  return {
    ...block,
    ...(withParameters.parameters
      ? {
          parameters: withParameters.parameters.map((parameter) => ({
            ...parameter,
            key: rename(parameter.key),
          })),
        }
      : {}),
    ...(withParameters.parameter_keys
      ? { parameter_keys: withParameters.parameter_keys.map(rename) }
      : {}),
    ...(withParameters.navigation_goal
      ? {
          navigation_goal: substituteGoalTokens(
            withParameters.navigation_goal,
            keyByToken,
          ),
        }
      : {}),
  } as WorkflowBlock;
}

type ApplyRecordedBlocksArgs = {
  nodes: Array<AppNode>;
  edges: Array<Edge>;
  recordedBlocks: Array<WorkflowBlock>;
  recordedInsertionPoint: InsertionPoint;
  recordedParameters: Array<RecordedParameter> | null;
  existingParameters: ParametersState;
};

type ApplyRecordedBlocksResult = {
  nodes: Array<AppNode>;
  edges: Array<Edge>;
  newParameters: ParametersState;
};

function applyRecordedBlocksToGraph({
  nodes,
  edges,
  recordedBlocks,
  recordedInsertionPoint,
  recordedParameters,
  existingParameters,
}: ApplyRecordedBlocksArgs): ApplyRecordedBlocksResult {
  const { previous, next, parent, connectingEdgeType } = recordedInsertionPoint;

  const newNodes: Array<AppNode> = [];
  const newEdges: Array<Edge> = [];

  const credentialKeyByToken = allocateCredentialKeys(
    recordedParameters ?? [],
    existingParameters,
  );
  const blocks =
    credentialKeyByToken.size > 0
      ? recordedBlocks.map((block) =>
          substituteCredentialTokens(block, credentialKeyByToken),
        )
      : recordedBlocks;

  let existingLabels = nodes
    .filter(isWorkflowBlockNode)
    .map((node) => node.data.label);

  let prevNodeId = previous;

  blocks.forEach((block, index) => {
    const id = nanoid();
    const label = generateNodeLabel(existingLabels);
    existingLabels = [...existingLabels, label];
    const blockWithLabel = { ...block, label: block.label || label };

    const node = convertToNode({ id, parentId: parent }, blockWithLabel, true);
    newNodes.push(node);

    if (prevNodeId) {
      newEdges.push({
        id: nanoid(),
        type: "edgeWithAddButton",
        source: prevNodeId,
        target: id,
        style: { strokeWidth: 2 },
      });
    }

    if (index === blocks.length - 1 && next) {
      newEdges.push({
        id: nanoid(),
        type: connectingEdgeType,
        source: id,
        target: next,
        style: { strokeWidth: 2 },
      });
    }

    prevNodeId = id;
  });

  const editedEdges = previous
    ? edges.filter((edge) => edge.source !== previous)
    : edges;

  const previousNode = nodes.find((node) => node.id === previous);
  const previousNodeIndex = previousNode
    ? nodes.indexOf(previousNode)
    : nodes.length - 1;

  const mergedNodes = [
    ...nodes.slice(0, previousNodeIndex + 1),
    ...newNodes,
    ...nodes.slice(previousNodeIndex + 1),
  ];

  const newParameters: ParametersState = [];

  for (const newParameter of recordedParameters ?? []) {
    const key = credentialKeyByToken.get(newParameter.key) ?? newParameter.key;
    const exists = existingParameters.some((param) => param.key === key);

    if (exists) {
      continue;
    }

    if (newParameter.parameter_type === "credential") {
      newParameters.push({
        key,
        parameterType: "credential",
        credentialId: newParameter.credential_id,
        description: newParameter.description ?? null,
      });
      continue;
    }

    newParameters.push({
      key,
      parameterType: "workflow",
      dataType: newParameter.workflow_parameter_type,
      description: newParameter.description ?? null,
      defaultValue: newParameter.default_value ?? "",
    });
  }

  return {
    nodes: mergedNodes,
    edges: [...editedEdges, ...newEdges],
    newParameters,
  };
}

export { applyRecordedBlocksToGraph };
export type { ApplyRecordedBlocksArgs, ApplyRecordedBlocksResult };
