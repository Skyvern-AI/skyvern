import { useReactFlow } from "@xyflow/react";
import { AppNode, isWorkflowBlockNode } from "../editor/nodes";
import {
  getUniqueLabelForExistingNode,
  getUpdatedNodesAfterLabelUpdateForParameterKeys,
  getUpdatedParametersAfterLabelUpdateForSourceParameterKey,
} from "../editor/workflowEditorUtils";
import { useCallback, useLayoutEffect, useState } from "react";
import {
  deferredEdits,
  recordDeferredEditReplay,
} from "@/hooks/useDeferredLockedEdit";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { toast } from "@/components/ui/use-toast";
import { useNodeCollapseStore } from "../editor/collapse/useNodeCollapseStore";
import { useWorkflowScopeId } from "../editor/WorkflowScopeContext";
import {
  isEditorMutationLocked,
  selectEditorMutationLocked,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";

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
  const { getNodes, setNodes } = useReactFlow();
  const mutationLocked = useWorkflowYamlEditorStore(selectEditorMutationLocked);
  const workflowId = useWorkflowScopeId() ?? "__global__";
  const deferKey = JSON.stringify([workflowId, id, "label"]);

  const handleLabelChange = useCallback(
    (value: string) => {
      const nodes = getNodes() as AppNode[];
      const node = nodes.find((node) => node.id === id);
      if (!node || !isWorkflowBlockNode(node)) return;
      if (isEditorMutationLocked()) {
        deferredEdits.set(deferKey, {
          value,
          propValue: node.data.label,
          propChanged: false,
          workflowId,
        });
        return;
      }
      deferredEdits.delete(deferKey);
      const {
        parameters: workflowParameters,
        setParameters: setWorkflowParameters,
      } = useWorkflowParametersStore.getState();
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
      const oldLabel = nodes.find((node) => node.id === id)?.data.label;
      if (oldLabel && oldLabel !== newLabel) {
        useNodeCollapseStore
          .getState()
          .renameBlock(workflowId, oldLabel, newLabel);
      }

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
    },
    [deferKey, getNodes, id, setNodes, workflowId],
  );

  useLayoutEffect(() => {
    if (isEditorMutationLocked()) return;
    const pending = deferredEdits.get(deferKey);
    deferredEdits.delete(deferKey);
    // A transaction may replace or rename the node; never overwrite that label.
    if (
      pending &&
      pending.workflowId === workflowId &&
      getNodes().find((node) => node.id === id)?.data.label ===
        pending.propValue
    ) {
      recordDeferredEditReplay(workflowId);
      handleLabelChange(pending.value);
    }
  }, [deferKey, getNodes, handleLabelChange, id, mutationLocked, workflowId]);

  return [label, handleLabelChange] as const;
}

export { useNodeLabelChangeHandler };
