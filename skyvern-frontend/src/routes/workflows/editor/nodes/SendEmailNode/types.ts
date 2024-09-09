import type { Node } from "@xyflow/react";

export type SendEmailNodeData = {
  recipients: string;
  subject: string;
  body: string;
  fileAttachments: string;
  editable: boolean;
  label: string;
  sender: string;
};

export type SendEmailNode = Node<SendEmailNodeData, "sendEmail">;

export const sendEmailNodeDefaultData: SendEmailNodeData = {
  recipients: "",
  subject: "",
  body: "",
  fileAttachments: "",
  editable: true,
  label: "",
  sender: "",
} as const;
