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
 * auto-generated key is taken.
 *
 * A recorded plain parameter is keyed from a field label, so it can read `credentials` and
 * collide with a credential key from either direction. Both are resolved here: a new
 * credential key never takes a recorded parameter's key, and a recorded parameter that lands
 * on a credential key is renamed. Sharing a key would make `str(key)` in the recorded code
 * stringify the credential — its password with it — into a page field.
 */
function nextFreeKey(base: string, taken: Set<string>): string {
  let suffix = 2;
  let candidate = `${base}_${suffix}`;
  while (taken.has(candidate)) {
    suffix += 1;
    candidate = `${base}_${suffix}`;
  }
  return candidate;
}

function allocateRecordedKeys(
  recordedParameters: Array<RecordedParameter>,
  existingParameters: ParametersState,
): Map<string, string> {
  const keyByToken = new Map<string, string>();
  const recordedPlainKeys = recordedParameters
    .filter((parameter) => parameter.parameter_type !== "credential")
    .map((parameter) => parameter.key);
  const takenKeys = new Set([
    ...existingParameters.map((parameter) => parameter.key),
    ...recordedPlainKeys,
  ]);
  const credentialKeys = new Set(
    existingParameters
      .filter((parameter) => parameter.parameterType === "credential")
      .map((parameter) => parameter.key),
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
    credentialKeys.add(key);
    keyByToken.set(parameter.key, key);
  }

  for (const key of recordedPlainKeys) {
    if (!credentialKeys.has(key)) {
      continue;
    }
    const renamed = nextFreeKey(key, takenKeys);
    takenKeys.add(renamed);
    keyByToken.set(key, renamed);
  }

  return keyByToken;
}

/**
 * One alternation over every token rather than a replacement per token: an allocated key can
 * itself be another token (a user-authored parameter may be named after a credential id), and
 * sequential passes would rewrite the first substitution again and point it at the wrong
 * credential.
 */
function tokenPattern(
  keyByToken: Map<string, string>,
  wrap: (alternation: string) => string,
): RegExp {
  const alternation = [...keyByToken.keys()]
    .map((token) => token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join("|");
  return new RegExp(wrap(alternation), "g");
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
  const pattern = tokenPattern(
    keyByToken,
    (alternation) => `(\\{\\{\\s*)(?:${alternation})\\b`,
  );
  return navigationGoal.replace(pattern, (match, prefix: string) => {
    const key = keyByToken.get(match.slice(prefix.length));
    return key === undefined ? match : `${prefix}${key}`;
  });
}

/**
 * A recorded code block reads the credential through the token as a Python identifier —
 * `await page.locator("#pw").fill(cred_123.password)` — so the code moves with the
 * declaration. A credential id is never a substring of another identifier, so the
 * word-boundary match is exact.
 */
function substituteCodeTokens(
  code: string,
  keyByToken: Map<string, string>,
): string {
  const pattern = tokenPattern(
    keyByToken,
    (alternation) => `\\b(?:${alternation})\\b`,
  );
  return code.replace(pattern, (match) => keyByToken.get(match) ?? match);
}

function substituteCredentialTokens(
  block: WorkflowBlock,
  keyByToken: Map<string, string>,
): WorkflowBlock {
  const rename = (key: string) => keyByToken.get(key) ?? key;
  // One structural cast rather than a switch over every block variant: only these four
  // fields are touched, and they carry the same meaning on every variant that has them.
  const withParameters = block as WorkflowBlock & {
    parameters?: Array<{ key: string }>;
    parameter_keys?: Array<string>;
    navigation_goal?: string | null;
    code?: string;
  };

  if (
    !withParameters.parameters &&
    !withParameters.parameter_keys &&
    !withParameters.navigation_goal &&
    !withParameters.code
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
    ...(withParameters.code
      ? { code: substituteCodeTokens(withParameters.code, keyByToken) }
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

  const credentialKeyByToken = allocateRecordedKeys(
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
    const baseLabel = block.label || generateNodeLabel(existingLabels);
    let label = baseLabel;
    let suffix = 2;
    while (existingLabels.includes(label)) {
      label = `${baseLabel}_${suffix}`;
      suffix += 1;
    }
    existingLabels = [...existingLabels, label];
    const blockWithLabel = { ...block, label };

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
