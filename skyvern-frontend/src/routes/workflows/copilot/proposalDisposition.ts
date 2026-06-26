import type { WorkflowCopilotStreamResponseUpdate } from "./workflowCopilotTypes";

export const shouldAutoApplyWorkflowResponse = (
  response: WorkflowCopilotStreamResponseUpdate,
  autoAccept: boolean,
  userCancelledThisTurn: boolean,
) => {
  return Boolean(
    response.updated_workflow &&
    (autoAccept || response.workflow_applied === true) &&
    response.proposal_disposition === "auto_applicable" &&
    !response.cancelled &&
    !userCancelledThisTurn,
  );
};
