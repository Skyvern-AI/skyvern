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
  it("auto-applies only what the backend says it applied", () => {
    expect(
      shouldAutoApplyWorkflowResponse(response({ workflow_applied: true })),
    ).toBe(true);
  });

  it("keeps a verified fix pending when the backend did not apply it", () => {
    // The server owns this: Turn off can commit there before the browser sees its request resolve.
    expect(
      shouldAutoApplyWorkflowResponse(response({ workflow_applied: false })),
    ).toBe(false);
  });

  it("does not auto-apply a frame that carries no workflow_applied at all", () => {
    // Every frame the backend emits carries the field; without it there is no authority to apply.
    const frame = response() as unknown as Record<string, unknown>;
    delete frame.workflow_applied;
    expect(
      shouldAutoApplyWorkflowResponse(
        frame as unknown as Parameters<
          typeof shouldAutoApplyWorkflowResponse
        >[0],
      ),
    ).toBe(false);
  });

  it.each(["review_untested", "review_tested"] as const)(
    "forces explicit review for %s proposals",
    (proposal_disposition) => {
      expect(
        shouldAutoApplyWorkflowResponse(response({ proposal_disposition })),
      ).toBe(false);
    },
  );

  it("does not auto-apply no_proposal responses", () => {
    expect(
      shouldAutoApplyWorkflowResponse(
        response({ proposal_disposition: "no_proposal" }),
      ),
    ).toBe(false);
  });

  it("honors a server commit over cancellation metadata", () => {
    expect(
      shouldAutoApplyWorkflowResponse(
        response({ workflow_applied: true, cancelled: true }),
      ),
    ).toBe(true);
  });

  it("does not auto-apply cancelled turns", () => {
    expect(shouldAutoApplyWorkflowResponse(response({ cancelled: true }))).toBe(
      false,
    );
    expect(shouldAutoApplyWorkflowResponse(response())).toBe(false);
  });
});
