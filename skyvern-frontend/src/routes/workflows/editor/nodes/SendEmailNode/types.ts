import type { Node } from "@xyflow/react";
import {
  EMAIL_BLOCK_SENDER,
  SKYVERN_DOWNLOAD_DIRECTORY,
  SMTP_HOST_PARAMETER_KEY,
  SMTP_PASSWORD_PARAMETER_KEY,
  SMTP_PORT_PARAMETER_KEY,
  SMTP_USERNAME_PARAMETER_KEY,
} from "../../constants";
import { NodeBaseData } from "../types";

export type SendEmailNodeData = NodeBaseData & {
  recipients: string;
  subject: string;
  body: string;
  fileAttachments: string;
  sender: string;
  smtpHostSecretParameterKey?: string;
  smtpPortSecretParameterKey?: string;
  smtpUsernameSecretParameterKey?: string;
  smtpPasswordSecretParameterKey?: string;
};

export type SendEmailNode = Node<SendEmailNodeData, "sendEmail">;

export const sendEmailNodeDefaultData: SendEmailNodeData = {
  recipients: "",
  subject: "",
  body: "",
  fileAttachments: SKYVERN_DOWNLOAD_DIRECTORY,
  editable: true,
  label: "",
  sender: EMAIL_BLOCK_SENDER,
  smtpHostSecretParameterKey: SMTP_HOST_PARAMETER_KEY,
  smtpPortSecretParameterKey: SMTP_PORT_PARAMETER_KEY,
  smtpUsernameSecretParameterKey: SMTP_USERNAME_PARAMETER_KEY,
  smtpPasswordSecretParameterKey: SMTP_PASSWORD_PARAMETER_KEY,
  continueOnFailure: false,
} as const;

export const helpTooltipContent = {
  fileAttachments:
    "Since we're in beta this section isn't fully customizable yet, contact us if you'd like to integrate it into your workflow.",
} as const;
