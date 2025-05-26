import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { RunEngine } from "@/api/types";

export type ExtractionNodeData = NodeBaseData & {
  url: string;
  dataExtractionGoal: string;
  dataSchema: string;
  maxRetries: number | null;
  maxStepsOverride: number | null;
  parameterKeys: Array<string>;
  cacheActions: boolean;
  engine: RunEngine | null;
};

export type ExtractionNode = Node<ExtractionNodeData, "extraction">;

export const extractionNodeDefaultData: ExtractionNodeData = {
  label: "",
  url: "",
  dataExtractionGoal: "",
  dataSchema: "null",
  maxRetries: null,
  maxStepsOverride: null,
  editable: true,
  parameterKeys: [],
  continueOnFailure: false,
  cacheActions: false,
  engine: RunEngine.SkyvernV1,
} as const;

export function isExtractionNode(node: Node): node is ExtractionNode {
  return node.type === "extraction";
}
