import type { Node } from "@xyflow/react";
import {
  EMAIL_BLOCK_SENDER,
  SMTP_HOST_PARAMETER_KEY,
  SMTP_PASSWORD_PARAMETER_KEY,
  SMTP_PORT_PARAMETER_KEY,
  SMTP_USERNAME_PARAMETER_KEY,
} from "../../constants";
import { NodeBaseData } from "../types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

export type HumanInteractionNodeData = NodeBaseData & {
  instructions: string;
  positiveDescriptor: string;
  negativeDescriptor: string;
  timeoutSeconds: number;
  recipients: string;
  subject: string;
  body: string;
  sender: string;
  smtpHostSecretParameterKey?: string;
  smtpPortSecretParameterKey?: string;
  smtpUsernameSecretParameterKey?: string;
  smtpPasswordSecretParameterKey?: string;
};

export type HumanInteractionNode = Node<
  HumanInteractionNodeData,
  "human_interaction"
>;

export const humanInteractionNodeDefaultData: HumanInteractionNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("human_interaction"),
  instructions: "Please review and approve or reject to continue the workflow.",
  positiveDescriptor: "Approve",
  negativeDescriptor: "Reject",
  timeoutSeconds: 60 * 60 * 2, // two hours
  recipients: "",
  subject: "Human interaction required for workflow run",
  body: "Your interaction is required for a workflow run!",
  editable: true,
  label: "",
  sender: EMAIL_BLOCK_SENDER,
  smtpHostSecretParameterKey: SMTP_HOST_PARAMETER_KEY,
  smtpPortSecretParameterKey: SMTP_PORT_PARAMETER_KEY,
  smtpUsernameSecretParameterKey: SMTP_USERNAME_PARAMETER_KEY,
  smtpPasswordSecretParameterKey: SMTP_PASSWORD_PARAMETER_KEY,
  continueOnFailure: false,
  model: null,
} as const;

export function isHumanInteractionNode(
  node: Node,
): node is HumanInteractionNode {
  return node.type === "human_interaction";
}
