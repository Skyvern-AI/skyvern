import type { Node } from "@xyflow/react";
import type { ParametersState } from "../types";
import { getAffectedBlocks } from "../jinjaReferences";

export type DocInputChip = {
  key: string;
  value: string | null;
  missing: boolean;
  unused: boolean;
};

const shown = (value: unknown): string | null => {
  if (value === null || value === undefined || value === "") return null;
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.replace(/^https?:\/\//, "");
};

export function getDocInputChips(
  parameters: ParametersState,
  nodes: Node[],
): DocInputChip[] {
  return parameters
    .filter((p) => p.parameterType !== "context")
    .map((p) => {
      const isWorkflowInput = p.parameterType === "workflow";
      const value = isWorkflowInput ? shown(p.defaultValue) : "saved";
      const used =
        getAffectedBlocks(nodes, p.key).length > 0 ||
        nodes.some((n) => n.type === "loop" && n.data.loopValue === p.key) ||
        parameters.some(
          (other) =>
            other.parameterType === "context" &&
            other.sourceParameterKey === p.key,
        );
      return {
        key: p.key,
        value,
        missing: isWorkflowInput && value === null,
        unused: !used,
      };
    });
}
