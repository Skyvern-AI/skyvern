import { useNodes, useReactFlow } from "@xyflow/react";
import { AppNode, isWorkflowBlockNode } from "../editor/nodes";
import {
  getUniqueLabelForExistingNode,
  getUpdatedNodesAfterLabelUpdateForParameterKeys,
  getUpdatedParametersAfterLabelUpdateForSourceParameterKey,
} from "../editor/workflowEditorUtils";
import { useState } from "react";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";

type Props = {
  id: string;
  initialValue: string;
};

function useNodeLabelChangeHandler({ id, initialValue }: Props) {
  const [label, setLabel] = useState(initialValue);
  const nodes = useNodes<AppNode>();
  const { setNodes } = useReactFlow();
  const {
    parameters: workflowParameters,
    setParameters: setWorkflowParameters,
  } = useWorkflowParametersStore();

  function handleLabelChange(value: string) {
    const existingLabels = nodes
      .filter(isWorkflowBlockNode)
      .map((n) => n.data.label);
    const labelWithoutWhitespace = value.replace(/\s+/g, "_");
    const newLabel = getUniqueLabelForExistingNode(
      labelWithoutWhitespace,
      existingLabels,
    );
    setLabel(newLabel);
    setNodes(
      getUpdatedNodesAfterLabelUpdateForParameterKeys(
        id,
        newLabel,
        nodes as Array<AppNode>,
      ),
    );
    setWorkflowParameters(
      getUpdatedParametersAfterLabelUpdateForSourceParameterKey(
        id,
        newLabel,
        nodes,
        workflowParameters,
      ),
    );
  }

  return [label, handleLabelChange] as const;
}

export { useNodeLabelChangeHandler };
