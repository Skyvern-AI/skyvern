import type { Node } from "@xyflow/react";

export type SendEmailNodeData = {
  recipients: string[];
  subject: string;
  body: string;
  fileAttachments: string[] | null;
  editable: boolean;
  label: string;
};

export type SendEmailNode = Node<SendEmailNodeData, "sendEmail">;
