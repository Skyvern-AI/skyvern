import { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

export const MAX_STEPS_DEFAULT = 25;
export const MAX_SCREENSHOT_SCROLLS_DEFAULT = 3;

export type Taskv2NodeData = NodeBaseData & {
  prompt: string;
  url: string;
  totpVerificationUrl: string | null;
  totpIdentifier: string | null;
  maxSteps: number | null;
  disableCache: boolean;
  maxScreenshotScrolls: number | null;
};

export type Taskv2Node = Node<Taskv2NodeData, "taskv2">;

export const taskv2NodeDefaultData: Taskv2NodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("task_v2"),
  label: "",
  continueOnFailure: false,
  editable: true,
  prompt: "",
  url: "",
  totpIdentifier: null,
  totpVerificationUrl: null,
  maxSteps: MAX_STEPS_DEFAULT,
  disableCache: false,
  model: null,
  maxScreenshotScrolls: null,
};

export function isTaskV2Node(node: Node): node is Taskv2Node {
  return node.type === "taskv2";
}
