import { useNodes, useReactFlow } from "@xyflow/react";
import { AppNode } from "../editor/nodes";
import {
  getUniqueLabelForExistingNode,
  getUpdatedNodesAfterLabelUpdateForParameterKeys,
  getUpdatedParametersAfterLabelUpdateForSourceParameterKey,
} from "../editor/workflowEditorUtils";
import { useState } from "react";
import { useWorkflowParametersState } from "../editor/useWorkflowParametersState";

type Props = {
  id: string;
  initialValue: string;
};

function useNodeLabelChangeHandler({ id, initialValue }: Props) {
  const [label, setLabel] = useState(initialValue);
  const nodes = useNodes<AppNode>();
  const { setNodes } = useReactFlow();
  const [workflowParameters, setWorkflowParameters] =
    useWorkflowParametersState();

  function handleLabelChange(value: string) {
    const existingLabels = nodes.map((n) => n.data.label);
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
