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
  extraHttpHeaders: string | null;
  editable: boolean;
};

export type OtherStartNodeData = {
  withWorkflowSettings: false;
  editable: boolean;
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
