import type { Node } from "@xyflow/react";
import { NodeBaseData } from "../types";
import { debuggableWorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";

export type EmailClient = "gmail" | "outlook";

export type EmailInboxNodeData = NodeBaseData & {
  emailClient: EmailClient;
  credentialId: string;
  folder: string;
  prompt: string;
  sender: string;
  subject: string;
  newerThanDays: number | null;
  maxResults: number;
  includeBody: boolean;
  parameterKeys: Array<string>;
};

export type EmailInboxNode = Node<EmailInboxNodeData, "emailInbox">;

export const emailInboxNodeDefaultData: EmailInboxNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("email_inbox"),
  label: "",
  continueOnFailure: false,
  editable: true,
  model: null,
  emailClient: "gmail",
  credentialId: "",
  folder: "INBOX",
  prompt: "",
  sender: "",
  subject: "",
  newerThanDays: null,
  maxResults: 25,
  includeBody: true,
  parameterKeys: [],
};

export function isEmailInboxNode(node: Node): node is EmailInboxNode {
  return node.type === "emailInbox";
}
