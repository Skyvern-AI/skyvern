import type { Node } from "@xyflow/react";
import type { NodeBaseData } from "../types";

export type HttpRequestNodeData = NodeBaseData & {
  curlCommand: string | null;
  method: string;
  url: string | null;
  headers: Record<string, string> | null;
  body: Record<string, unknown> | string | null;
  timeout: number;
  followRedirects: boolean;
  parameterKeys: Array<string>;
  inputMode: "curl" | "manual";
};

export type HttpRequestNode = Node<HttpRequestNodeData, "httpRequest">;

export const httpRequestNodeDefaultData: HttpRequestNodeData = {
  label: "",
  continueOnFailure: false,
  editable: true,
  model: null,
  curlCommand: null,
  method: "GET",
  url: null,
  headers: null,
  body: null,
  timeout: 30,
  followRedirects: true,
  parameterKeys: [],
  inputMode: "manual",
};
