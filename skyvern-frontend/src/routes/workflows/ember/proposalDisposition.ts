import type { EmberStreamResponseUpdate } from "./emberTypes";

export const shouldAutoApplyWorkflowResponse = (
  response: EmberStreamResponseUpdate,
) => {
  return Boolean(
    response.updated_workflow &&
    // The server decides: it wrote the row and committed (or did not commit) canonical for this turn.
    response.workflow_applied === true,
  );
};
