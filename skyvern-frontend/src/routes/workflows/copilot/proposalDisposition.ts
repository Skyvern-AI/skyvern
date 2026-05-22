import type { WorkflowCopilotStreamResponseUpdate } from "./workflowCopilotTypes";

export const shouldAutoApplyWorkflowResponse = (
  response: WorkflowCopilotStreamResponseUpdate,
  autoAccept: boolean,
  userCancelledThisTurn: boolean,
) => {
  const autoApplicable =
    response.proposal_disposition !== undefined
      ? response.proposal_disposition === "auto_applicable"
      : !response.unvalidated && !response.force_review;
  return Boolean(
    response.updated_workflow &&
    autoAccept &&
    autoApplicable &&
    !response.cancelled &&
    !userCancelledThisTurn,
  );
};
