import type { Node } from "@xyflow/react";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";
import type { NodeBaseData } from "../types";

export type WebSearchNodeData = NodeBaseData & {
  query: string;
  provider: "auto" | "google" | "exa";
  numResults: number;
  prompt: string;
  jsonSchema: string;
  parameterKeys: Array<string>;
};

export type WebSearchNode = Node<WebSearchNodeData, "web_search">;

export const webSearchNodeDefaultData: WebSearchNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("web_search"),
  label: "",
  continueOnFailure: false,
  editable: true,
  model: null,
  query: "",
  provider: "auto",
  numResults: 10,
  prompt: "",
  jsonSchema: "null",
  parameterKeys: [],
};
