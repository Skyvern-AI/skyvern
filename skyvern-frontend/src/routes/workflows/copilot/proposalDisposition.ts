import type { WorkflowCopilotStreamResponseUpdate } from "./workflowCopilotTypes";

export const shouldAutoApplyWorkflowResponse = (
  response: WorkflowCopilotStreamResponseUpdate,
  userCancelledThisTurn: boolean,
) => {
  return Boolean(
    response.updated_workflow &&
    // The server decides: it wrote the row and committed (or did not commit) canonical for this turn.
    response.workflow_applied === true &&
    response.proposal_disposition === "auto_applicable" &&
    !response.cancelled &&
    !userCancelledThisTurn,
  );
};
