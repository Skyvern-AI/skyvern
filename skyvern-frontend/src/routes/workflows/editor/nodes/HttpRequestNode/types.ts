import type { Node } from "@xyflow/react";
import type { NodeBaseData } from "../types";
import type { WorkflowParameter } from "../../../types/workflowTypes";

export type HttpRequestNodeData = {
  editable: boolean;
  label: string;
  inputMode?: "manual" | "curl";
  curlCommand?: string;
  method?: string;
  url?: string;
  headers?: Record<string, string>;
  body?: string | Record<string, unknown>;
  timeout?: number;
  followRedirects?: boolean;
  parameterKeys?: Array<string>;
  continueOnFailure?: boolean;
};

export type HttpRequestNode = Node<HttpRequestNodeData, "httpRequest">;

export const httpRequestNodeDefaultData: HttpRequestNodeData = {
  label: "",
  continueOnFailure: false,
  editable: true,
  curlCommand: "",
  method: "GET",
  url: "",
  headers: {},
  body: "",
  timeout: 30,
  followRedirects: true,
  parameterKeys: [],
  inputMode: "manual",
};