import type { Node } from "@xyflow/react";
import type { ParametersState } from "../types";
import { getAffectedBlocks } from "../jinjaReferences";
import { docInputValuePreview } from "./docValuePreview";

export type DocInputChip = {
  key: string;
  value: string | null;
  valueTitle?: string;
  missing: boolean;
  unused: boolean;
};

export const NOT_USED_TITLE = "Not used by any step";

export function getDocInputChips(
  parameters: ParametersState,
  nodes: Node[],
): DocInputChip[] {
  return parameters.map((p) => {
    const isWorkflowInput = p.parameterType === "workflow";
    const preview = isWorkflowInput
      ? docInputValuePreview(p.defaultValue)
      : null;
    const value = isWorkflowInput
      ? (preview?.text ?? null)
      : p.parameterType === "context"
        ? `from ${p.sourceParameterKey}`
        : "credential";
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
      // The value's own title hides the chip's, so it repeats the unused note.
      valueTitle:
        preview?.title && !used
          ? `${preview.title}\n${NOT_USED_TITLE}`
          : preview?.title,
      missing: isWorkflowInput && value === null,
      unused: !used,
    };
  });
}
