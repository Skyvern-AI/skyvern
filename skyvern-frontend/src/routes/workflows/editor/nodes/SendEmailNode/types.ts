import type { Node } from "@xyflow/react";
import {
  EMAIL_BLOCK_SENDER,
  SKYVERN_DOWNLOAD_DIRECTORY,
} from "../../constants";
import { NodeBaseData } from "../types";
import {
  type EmailBodyFormat,
  debuggableWorkflowBlockTypes,
} from "@/routes/workflows/types/workflowTypes";

export type SendEmailNodeData = NodeBaseData & {
  recipients: string;
  subject: string;
  body: string;
  bodyFormat: EmailBodyFormat;
  fileAttachments: string;
  sender: string;
  smtpHostSecretParameterKey?: string;
  smtpPortSecretParameterKey?: string;
  smtpUsernameSecretParameterKey?: string;
  smtpPasswordSecretParameterKey?: string;
  customSmtpHost: string | null;
  customSmtpPort: string | null;
  customSmtpUsername: string | null;
  customSmtpPassword: string | null;
};

export type SendEmailNode = Node<SendEmailNodeData, "sendEmail">;

export const sendEmailNodeDefaultData: SendEmailNodeData = {
  debuggable: debuggableWorkflowBlockTypes.has("send_email"),
  recipients: "",
  subject: "",
  body: "",
  bodyFormat: "text",
  fileAttachments: SKYVERN_DOWNLOAD_DIRECTORY,
  editable: true,
  label: "",
  sender: EMAIL_BLOCK_SENDER,
  customSmtpHost: null,
  customSmtpPort: null,
  customSmtpUsername: null,
  customSmtpPassword: null,
  continueOnFailure: false,
  model: null,
} as const;
