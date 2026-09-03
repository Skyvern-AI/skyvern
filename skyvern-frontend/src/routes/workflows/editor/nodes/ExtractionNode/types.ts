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
  // Export the extracted data as a Parquet file -- an output option of this
  // block rather than a separate Data Export block.
  exportEnabled: boolean;
  exportDataSchema: string;
  exportFileName: string;
  exportRecords: string;
};

export type ExtractionNode = Node<ExtractionNodeData, "extraction">;

export const extractionExportDataSchemaDefault = JSON.stringify(
  {
    type: "array",
    items: {
      type: "object",
      properties: { value: { type: "string" } },
    },
  },
  null,
  2,
);

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
  ignoreWorkflowSystemPrompt: false,
  exportEnabled: false,
  exportDataSchema: extractionExportDataSchemaDefault,
  exportFileName: "",
  exportRecords: "",
} as const;

export function isExtractionNode(node: Node): node is ExtractionNode {
  return node.type === "extraction";
}
