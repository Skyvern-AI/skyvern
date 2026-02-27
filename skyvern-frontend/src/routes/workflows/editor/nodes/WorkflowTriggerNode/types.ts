import { Node } from "@xyflow/react";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";
import { NodeBaseData } from "../types";

export type WorkflowTriggerNodeData = NodeBaseData & {
  workflowPermanentId: string;
  payload: string; // JSON string of the payload dict
  waitForCompletion: boolean;
  browserSessionId: string;
  useParentBrowserSession: boolean;
  parameterKeys: Array<string>;
};

export type WorkflowTriggerNode = Node<
  WorkflowTriggerNodeData,
  "workflowTrigger"
>;

export const workflowTriggerNodeDefaultData: WorkflowTriggerNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("workflow_trigger"),
  label: "",
  continueOnFailure: false,
  editable: true,
  model: null,
  workflowPermanentId: "",
  payload: "{}",
  waitForCompletion: true,
  browserSessionId: "",
  useParentBrowserSession: false,
  parameterKeys: [],
};

export function isWorkflowTriggerNode(node: Node): node is WorkflowTriggerNode {
  return node.type === "workflowTrigger";
}
