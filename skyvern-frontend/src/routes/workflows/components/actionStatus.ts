import { ActionsApiResponse, ActionTypes, Status } from "@/api/types";

// A terminate action executes "successfully" (the backend persists it with
// status "completed"), but it means the agent gave up — show it as a failure.
// Every other status passes through unchanged.
export function getActionDisplayStatus(action: ActionsApiResponse): Status {
  if (action.action_type === ActionTypes.terminate) {
    return Status.Terminated;
  }
  return action.status;
}

export function isActionSuccess(action: ActionsApiResponse): boolean {
  // terminate is not a success even though it is persisted as completed — it is
  // its own "terminated" outcome (see getActionDisplayKind for the distinction).
  if (action.action_type === ActionTypes.terminate) {
    return false;
  }
  // wait reports ActionFailure from the backend, but completing a wait is
  // expected, not a failure.
  if (action.action_type === ActionTypes.wait) {
    return true;
  }
  return action.status === Status.Completed || action.status === Status.Skipped;
}

export type ActionDisplayKind = "success" | "failure" | "terminated";

// terminate is its own outcome — the agent deliberately gave up — so it gets a
// distinct visual (matching the "terminated" status badge) rather than the red
// failure treatment used for errors.
export function getActionDisplayKind(
  action: ActionsApiResponse,
): ActionDisplayKind {
  if (action.action_type === ActionTypes.terminate) {
    return "terminated";
  }
  return isActionSuccess(action) ? "success" : "failure";
}
