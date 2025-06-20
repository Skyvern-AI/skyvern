import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";

export type HTTPNodeData = NodeBaseData & {
  curlCommand: string;
  method?: string;
  url?: string;
  headers?: Record<string, string>;
  body?: string;
  timeout?: number;
  parameterKeys: Array<string> | null;
};

export type HTTPNode = Node<HTTPNodeData, "http">;

export const httpNodeDefaultData: HTTPNodeData = {
  editable: true,
  label: "",
  curlCommand: "curl https://api.example.com",
  method: undefined,
  url: undefined,
  headers: undefined,
  body: undefined,
  timeout: 30,
  continueOnFailure: false,
  parameterKeys: null,
  model: null,
} as const;