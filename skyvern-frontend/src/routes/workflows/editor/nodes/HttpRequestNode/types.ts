import { Node } from "@xyflow/react";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";
import { NodeBaseData } from "../types";

export type HttpRequestNodeData = NodeBaseData & {
  method: string;
  url: string;
  headers: string; // JSON string representation of headers
  body: string; // JSON string representation of body
  files: string; // JSON string representation of files (dict mapping field names to file paths/URLs)
  timeout: number;
  followRedirects: boolean;
  parameterKeys: Array<string>;
};

export type HttpRequestNode = Node<HttpRequestNodeData, "http_request">;

export const httpRequestNodeDefaultData: HttpRequestNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("http_request"),
  label: "",
  continueOnFailure: false,
  method: "GET",
  url: "",
  headers: "{}",
  body: "{}",
  files: "{}",
  timeout: 30,
  followRedirects: true,
  parameterKeys: [],
  editable: true,
  model: null,
};

export function isHttpRequestNode(node: Node): node is HttpRequestNode {
  return node.type === "http_request";
}
