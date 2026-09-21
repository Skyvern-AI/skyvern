import type { Node } from "@xyflow/react";
import { EMAIL_BLOCK_SENDER } from "../../constants";
import { NodeBaseData } from "../types";
import {
  type EmailBodyFormat,
  debuggableWorkflowBlockTypes,
} from "@/routes/workflows/types/workflowTypes";

export type HumanInteractionNodeData = NodeBaseData & {
  instructions: string;
  positiveDescriptor: string;
  negativeDescriptor: string;
  timeoutSeconds: number;
  recipients: string;
  subject: string;
  body: string;
  bodyFormat: EmailBodyFormat;
  sender: string;
};

export type HumanInteractionNode = Node<
  HumanInteractionNodeData,
  "human_interaction"
>;

export const humanInteractionNodeDefaultData: HumanInteractionNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("human_interaction"),
  instructions: "Please review and approve or reject to continue the agent.",
  positiveDescriptor: "Approve",
  negativeDescriptor: "Reject",
  timeoutSeconds: 60 * 60 * 2, // two hours
  recipients: "",
  subject: "Human interaction required for agent run",
  body: "Your interaction is required for an agent run!",
  bodyFormat: "text",
  editable: true,
  label: "",
  sender: EMAIL_BLOCK_SENDER,
  continueOnFailure: false,
  model: null,
} as const;

export function isHumanInteractionNode(
  node: Node,
): node is HumanInteractionNode {
  return node.type === "human_interaction";
}
