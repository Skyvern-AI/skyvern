import { ProxyLocation } from "@/api/types";
import type { Node } from "@xyflow/react";
import { AppNode } from "..";
import { WorkflowModel } from "@/routes/workflows/types/workflowTypes";

export type WorkflowStartNodeData = {
  withWorkflowSettings: true;
  webhookCallbackUrl: string;
  proxyLocation: ProxyLocation;
  persistBrowserSession: boolean;
  model: WorkflowModel | null;
  maxScreenshotScrolls: number | null;
  extraHttpHeaders: string | Record<string, unknown> | null;
  editable: boolean;
  runWith: string | null;
  scriptCacheKey: string | null;
  aiFallback: boolean;
  runSequentially: boolean;
  sequentialKey: string | null;
  label: "__start_block__";
  showCode: boolean;
};

export type OtherStartNodeData = {
  withWorkflowSettings: false;
  editable: boolean;
  label: "__start_block__";
  showCode: boolean;
  parentNodeType?: "loop" | "conditional";
};

export type StartNodeData = WorkflowStartNodeData | OtherStartNodeData;

export type StartNode = Node<StartNodeData, "start">;

export function isStartNode(node: AppNode): node is StartNode {
  return node.type === "start";
}

export function isWorkflowStartNodeData(
  data: StartNodeData,
): data is WorkflowStartNodeData {
  return data.withWorkflowSettings;
}
