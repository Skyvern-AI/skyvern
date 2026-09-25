import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { refuseMutationDuringYamlCommit } from "@/store/WorkflowYamlEditorStore";
import { useReactFlow } from "@xyflow/react";
import { useCallback } from "react";

import { useWorkflowScopeReadOnly } from "./WorkflowScopeContext";

type UseUpdateOptions = {
  id: string;
  editable: boolean;
};

/**
 * A reusable hook for updating node data in React Flow.
 *
 * @template T - The root data type that extends Record<string, unknown>
 * @param options - Configuration object containing node id and editable flag
 * @returns An update function that accepts partial updates of type T
 *
 * @example
 * ```tsx
 * const update = useUpdate<WaitNode["data"]>({ id, editable });
 * update({ waitInSeconds: "5" });
 * ```
 */
export function useUpdate<T extends Record<string, unknown>>({
  id,
  editable,
}: UseUpdateOptions) {
  const { updateNodeData } = useReactFlow();
  // Comparison/diff canvases mount read-only; no control may persist to the reviewed snapshot.
  const readOnlyScope = useWorkflowScopeReadOnly();

  const update = useCallback(
    (updates: Partial<T>, options?: { source: "workflow" }) => {
      if (!editable || readOnlyScope || refuseMutationDuringYamlCommit())
        return false;
      if (options?.source !== "workflow")
        useWorkflowTitleStore.getState().recordCopilotGraphEdit();
      updateNodeData(id, updates);
      return true;
    },
    [id, editable, readOnlyScope, updateNodeData],
  );

  return update;
}
