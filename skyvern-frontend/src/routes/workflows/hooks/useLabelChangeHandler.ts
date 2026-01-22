import { useNodes, useReactFlow } from "@xyflow/react";
import { AppNode, isWorkflowBlockNode } from "../editor/nodes";
import {
  getUniqueLabelForExistingNode,
  getUpdatedNodesAfterLabelUpdateForParameterKeys,
  getUpdatedParametersAfterLabelUpdateForSourceParameterKey,
} from "../editor/workflowEditorUtils";
import { useState, useCallback } from "react";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import {
  validateBlockLabel,
  sanitizeBlockLabel,
} from "../editor/blockLabelValidation";

type Props = {
  id: string;
  initialValue: string;
};

function useNodeLabelChangeHandler({ id, initialValue }: Props) {
  const [label, setLabel] = useState(initialValue);
  const [validationError, setValidationError] = useState<string | null>(null);
  const nodes = useNodes<AppNode>();
  const { setNodes } = useReactFlow();
  const {
    parameters: workflowParameters,
    setParameters: setWorkflowParameters,
  } = useWorkflowParametersStore();

  const getExistingLabels = useCallback(() => {
    return nodes
      .filter(isWorkflowBlockNode)
      .filter((n) => n.id !== id)
      .map((n) => n.data.label);
  }, [nodes, id]);

  const validateLabel = useCallback(
    (value: string): string | null => {
      const sanitized = sanitizeBlockLabel(value);
      return validateBlockLabel(sanitized, {
        existingLabels: getExistingLabels(),
        currentLabel: label,
      });
    },
    [getExistingLabels, label],
  );

  function handleLabelChange(value: string) {
    const sanitized = sanitizeBlockLabel(value);

    // Validate the new label
    const error = validateLabel(sanitized);
    if (error) {
      setValidationError(error);
      return;
    }

    // Clear any previous validation error
    setValidationError(null);

    // If the value is unchanged after sanitization, don't update
    if (sanitized === label) {
      return;
    }

    // Get unique label in case of conflicts (fallback)
    const existingLabels = nodes
      .filter(isWorkflowBlockNode)
      .map((n) => n.data.label);
    const newLabel = getUniqueLabelForExistingNode(sanitized, existingLabels);

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

  return [label, handleLabelChange, validationError, validateLabel] as const;
}

export { useNodeLabelChangeHandler };
