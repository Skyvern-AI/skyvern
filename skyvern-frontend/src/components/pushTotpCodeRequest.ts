import type { OtpType as OtpTypeValue } from "@/api/types";

export type SendTotpCodeRequest = {
  totp_identifier: string;
  content: string;
  type: OtpTypeValue;
  workflow_run_id?: string;
  workflow_id?: string;
  task_id?: string;
  source?: string;
};

type BuildSendTotpCodeRequestInput = {
  identifier: string;
  content: string;
  otpType: OtpTypeValue;
  workflowRunId: string;
  workflowId: string;
  taskId: string;
};

export function buildSendTotpCodeRequest({
  identifier,
  content,
  otpType,
  workflowRunId,
  workflowId,
  taskId,
}: BuildSendTotpCodeRequestInput): SendTotpCodeRequest {
  const payload: SendTotpCodeRequest = {
    totp_identifier: identifier.trim(),
    content: content.trim(),
    type: otpType,
    source: "manual_ui",
  };

  const trimmedWorkflowRunId = workflowRunId.trim();
  const trimmedWorkflowId = workflowId.trim();
  const trimmedTaskId = taskId.trim();

  if (trimmedWorkflowRunId !== "") {
    payload.workflow_run_id = trimmedWorkflowRunId;
  }
  if (trimmedWorkflowId !== "") {
    payload.workflow_id = trimmedWorkflowId;
  }
  if (trimmedTaskId !== "") {
    payload.task_id = trimmedTaskId;
  }

  return payload;
}
