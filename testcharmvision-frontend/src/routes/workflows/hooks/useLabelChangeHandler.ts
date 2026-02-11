import { useNodes, useReactFlow } from "@xyflow/react";
import { AppNode, isWorkflowBlockNode } from "../editor/nodes";
import {
  getUniqueLabelForExistingNode,
  getUpdatedNodesAfterLabelUpdateForParameterKeys,
  getUpdatedParametersAfterLabelUpdateForSourceParameterKey,
} from "../editor/workflowEditorUtils";
import { useState } from "react";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { toast } from "@/components/ui/use-toast";

/**
 * Sanitizes a block label to be a valid Python/Jinja2 identifier.
 * Block labels are used to create output parameter keys (e.g., '{label}_output')
 * which are then used as Jinja2 template variable names.
 */
function sanitizeBlockLabel(value: string): {
  sanitized: string;
  wasModified: boolean;
} {
  const original = value;

  // Replace any character that's not a letter, digit, or underscore with underscore
  let sanitized = value.replace(/[^a-zA-Z0-9_]/g, "_");

  // Collapse multiple consecutive underscores into one
  sanitized = sanitized.replace(/_+/g, "_");

  // Remove leading/trailing underscores for cleaner labels
  sanitized = sanitized.replace(/^_+|_+$/g, "");

  // If starts with a digit (after cleanup), prepend an underscore
  if (/^[0-9]/.test(sanitized)) {
    sanitized = "_" + sanitized;
  }

  // If everything was stripped, provide a default
  if (!sanitized) {
    sanitized = "block";
  }

  return { sanitized, wasModified: original !== sanitized };
}

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
      .filter((n) => isWorkflowBlockNode(n) && n.id !== id)
      .map((n) => n.data.label);

    // Sanitize the label to be a valid Python identifier
    const { sanitized, wasModified } = sanitizeBlockLabel(value);

    // Show a toast if characters were modified
    if (wasModified) {
      toast({
        title: "Block label adjusted",
        description:
          "Block labels can only contain letters, numbers, and underscores. Invalid characters have been replaced.",
      });
    }

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

  return [label, handleLabelChange] as const;
}

export { useNodeLabelChangeHandler };
