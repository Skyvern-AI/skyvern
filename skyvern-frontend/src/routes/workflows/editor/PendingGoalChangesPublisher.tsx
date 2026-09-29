import { useNodes, useReactFlow } from "@xyflow/react";
import { useCallback, useEffect } from "react";

import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { refuseMutationDuringYamlCommit } from "@/store/WorkflowYamlEditorStore";

import { isWorkflowBlockNode, type AppNode } from "./nodes";
import {
  goalChangeUndoPatch,
  pendingGoalChangesOf,
} from "./workflowEditorUtils";

export function PendingGoalChangesPublisher() {
  const nodes = useNodes<AppNode>();
  const { getNodes, updateNodeData } = useReactFlow<AppNode>();
  const setPendingGoalChanges = useCopilotActionStore(
    (state) => state.setPendingGoalChanges,
  );
  const setUndoGoalChange = useCopilotActionStore(
    (state) => state.setUndoGoalChange,
  );

  const undoGoalChange = useCallback(
    (label: string) => {
      if (refuseMutationDuringYamlCommit()) {
        return;
      }
      const node = getNodes().find(
        (candidate) =>
          isWorkflowBlockNode(candidate) &&
          candidate.type === "codeBlock" &&
          candidate.data.label === label,
      );
      if (!node || node.type !== "codeBlock") {
        return;
      }
      const patch = goalChangeUndoPatch(node.data);
      if (patch) {
        useWorkflowTitleStore.getState().recordCopilotGraphEdit();
        updateNodeData(node.id, patch);
      }
    },
    [getNodes, updateNodeData],
  );

  useEffect(() => {
    setPendingGoalChanges(pendingGoalChangesOf(nodes));
  }, [nodes, setPendingGoalChanges]);
  useEffect(() => {
    setUndoGoalChange(undoGoalChange);
  }, [undoGoalChange, setUndoGoalChange]);
  useEffect(
    () => () => {
      setPendingGoalChanges([]);
      setUndoGoalChange(() => {});
    },
    [setPendingGoalChanges, setUndoGoalChange],
  );
  return null;
}
