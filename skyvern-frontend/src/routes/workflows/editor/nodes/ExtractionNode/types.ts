import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { RunEngine } from "@/api/types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

export type ExtractionNodeData = NodeBaseData & {
  url: string;
  dataExtractionGoal: string;
  dataSchema: string;
  maxRetries: number | null;
  maxStepsOverride: number | null;
  parameterKeys: Array<string>;
  disableCache: boolean;
  engine: RunEngine | null;
};

export type ExtractionNode = Node<ExtractionNodeData, "extraction">;

export const extractionNodeDefaultData: ExtractionNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("extraction"),
  label: "",
  url: "",
  dataExtractionGoal: "",
  dataSchema: "null",
  maxRetries: null,
  maxStepsOverride: null,
  editable: true,
  parameterKeys: [],
  continueOnFailure: false,
  disableCache: false,
  engine: RunEngine.SkyvernV1,
  model: null,
} as const;

export function isExtractionNode(node: Node): node is ExtractionNode {
  return node.type === "extraction";
}
