import { describe, expect, it } from "vitest";
import { OtpType } from "@/api/types";
import { buildSendTotpCodeRequest } from "./pushTotpCodeRequest";

describe("buildSendTotpCodeRequest", () => {
  it("includes explicit magic-link type and optional metadata", () => {
    expect(
      buildSendTotpCodeRequest({
        identifier: " user@example.com ",
        content: " https://example.com/login?token=abc ",
        otpType: OtpType.MagicLink,
        workflowRunId: " wr_123 ",
        workflowId: " wf_123 ",
        taskId: " tsk_123 ",
      }),
    ).toEqual({
      totp_identifier: "user@example.com",
      content: "https://example.com/login?token=abc",
      type: OtpType.MagicLink,
      source: "manual_ui",
      workflow_run_id: "wr_123",
      workflow_id: "wf_123",
      task_id: "tsk_123",
    });
  });

  it("omits blank optional metadata for numeric codes", () => {
    expect(
      buildSendTotpCodeRequest({
        identifier: " user@example.com ",
        content: " 123456 ",
        otpType: OtpType.Totp,
        workflowRunId: " ",
        workflowId: "",
        taskId: "",
      }),
    ).toEqual({
      totp_identifier: "user@example.com",
      content: "123456",
      type: OtpType.Totp,
      source: "manual_ui",
    });
  });
});
