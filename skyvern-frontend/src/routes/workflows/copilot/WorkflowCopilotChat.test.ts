import { describe, expect, it } from "vitest";

import { shouldAutoApplyWorkflowResponse } from "./proposalDisposition";
import type { WorkflowCopilotStreamResponseUpdate } from "./workflowCopilotTypes";

const response = (
  overrides: Partial<WorkflowCopilotStreamResponseUpdate> = {},
): WorkflowCopilotStreamResponseUpdate => ({
  type: "response",
  workflow_copilot_chat_id: "wcc_1",
  message: "done",
  updated_workflow: { workflow_id: "wf_1" } as never,
  response_time: "2026-05-21T00:00:00Z",
  proposal_disposition: "auto_applicable",
  ...overrides,
});

describe("shouldAutoApplyWorkflowResponse", () => {
  it("auto-applies auto_applicable proposals when auto accept is enabled", () => {
    expect(shouldAutoApplyWorkflowResponse(response(), true, false)).toBe(true);
  });

  it.each(["review_untested", "review_tested"] as const)(
    "forces explicit review for %s proposals",
    (proposal_disposition) => {
      expect(
        shouldAutoApplyWorkflowResponse(
          response({ proposal_disposition }),
          true,
          false,
        ),
      ).toBe(false);
    },
  );

  it("does not auto-apply no_proposal responses", () => {
    expect(
      shouldAutoApplyWorkflowResponse(
        response({ proposal_disposition: "no_proposal" }),
        true,
        false,
      ),
    ).toBe(false);
  });

  it("does not auto-apply cancelled turns", () => {
    expect(
      shouldAutoApplyWorkflowResponse(
        response({ cancelled: true }),
        true,
        false,
      ),
    ).toBe(false);
    expect(shouldAutoApplyWorkflowResponse(response(), true, true)).toBe(false);
  });
});
