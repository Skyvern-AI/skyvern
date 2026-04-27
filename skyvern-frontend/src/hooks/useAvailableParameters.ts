import { useEdges, useNodes } from "@xyflow/react";
import { useMemo } from "react";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { AppNode } from "@/routes/workflows/editor/nodes";
import {
  getAvailableOutputParameterKeys,
  isNodeInsideForLoop,
} from "@/routes/workflows/editor/workflowEditorUtils";
import {
  GLOBAL_RESERVED_PARAMETERS,
  LOOP_RESERVED_PARAMETERS,
} from "@/routes/workflows/editor/constants";

export type ParameterCategory = "parameter" | "output" | "system";

export type AvailableParameter = {
  key: string;
  category: ParameterCategory;
  description?: string;
};

function useAvailableParameters(nodeId: string): AvailableParameter[] {
  const { parameters: workflowParameters } = useWorkflowParametersStore();
  const nodes = useNodes<AppNode>();
  const edges = useEdges();

  return useMemo(() => {
    const items: AvailableParameter[] = [];

    for (const param of workflowParameters) {
      items.push({ key: param.key, category: "parameter" });
    }

    const outputKeys = getAvailableOutputParameterKeys(nodes, edges, nodeId);
    for (const key of outputKeys) {
      items.push({ key, category: "output" });
    }

    for (const param of GLOBAL_RESERVED_PARAMETERS) {
      items.push({
        key: param.key,
        category: "system",
        description: param.description,
      });
    }

    if (isNodeInsideForLoop(nodes, nodeId)) {
      for (const param of LOOP_RESERVED_PARAMETERS) {
        items.push({
          key: param.key,
          category: "system",
          description: param.description,
        });
      }
    }

    return items;
  }, [workflowParameters, nodes, edges, nodeId]);
}

export { useAvailableParameters };
