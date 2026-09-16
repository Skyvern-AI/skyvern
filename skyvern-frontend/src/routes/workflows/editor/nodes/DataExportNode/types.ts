import type { Node } from "@xyflow/react";

import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

import { NodeBaseData } from "../types";

export type DataExportNodeData = NodeBaseData & {
  data: string;
  dataSchema: string;
  fileName: string;
  parameterKeys: Array<string>;
};

export type DataExportNode = Node<DataExportNodeData, "dataExport">;

export const dataExportNodeDefaultData: DataExportNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("data_export"),
  editable: true,
  label: "",
  data: "[]",
  dataSchema: JSON.stringify(
    {
      type: "array",
      items: {
        type: "object",
        properties: { value: { type: "string" } },
      },
    },
    null,
    2,
  ),
  fileName: "",
  parameterKeys: [],
  continueOnFailure: false,
  model: null,
} as const;
