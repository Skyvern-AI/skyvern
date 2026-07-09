import { Edge } from "@xyflow/react";
import { nanoid } from "nanoid";

import type {
  WorkflowBlock,
  WorkflowParameter,
} from "@/routes/workflows/types/workflowTypes";
import type { InsertionPoint } from "@/store/RecordedBlocksStore";
import { AppNode, isWorkflowBlockNode } from "../nodes";
import { ParametersState } from "../types";
import { convertToNode, generateNodeLabel } from "../workflowEditorUtils";

type ApplyRecordedBlocksArgs = {
  nodes: Array<AppNode>;
  edges: Array<Edge>;
  recordedBlocks: Array<WorkflowBlock>;
  recordedInsertionPoint: InsertionPoint;
  recordedParameters: Array<WorkflowParameter> | null;
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

  let existingLabels = nodes
    .filter(isWorkflowBlockNode)
    .map((node) => node.data.label);

  let prevNodeId = previous;

  recordedBlocks.forEach((block, index) => {
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

    if (index === recordedBlocks.length - 1 && next) {
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
    const exists = existingParameters.some(
      (param) => param.key === newParameter.key,
    );

    if (!exists) {
      newParameters.push({
        key: newParameter.key,
        parameterType: "workflow",
        dataType: newParameter.workflow_parameter_type,
        description: newParameter.description ?? null,
        defaultValue: newParameter.default_value ?? "",
      });
    }
  }

  return {
    nodes: mergedNodes,
    edges: [...editedEdges, ...newEdges],
    newParameters,
  };
}

export { applyRecordedBlocksToGraph };
export type { ApplyRecordedBlocksArgs, ApplyRecordedBlocksResult };
