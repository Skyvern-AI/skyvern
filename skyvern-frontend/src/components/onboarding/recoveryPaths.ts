type RecoveryPathKind = "retry" | "navigate" | "external";

type RecoveryPathId =
  | "retry"
  | "edit_workflow"
  | "update_credentials"
  | "view_docs"
  | "contact_support";

type RecoveryPath = {
  id: RecoveryPathId;
  label: string;
  kind: RecoveryPathKind;
};

const RETRY: RecoveryPath = { id: "retry", label: "Retry run", kind: "retry" };
const EDIT_WORKFLOW: RecoveryPath = {
  id: "edit_workflow",
  label: "Edit workflow",
  kind: "navigate",
};
const UPDATE_CREDENTIALS: RecoveryPath = {
  id: "update_credentials",
  label: "Update credentials",
  kind: "navigate",
};
const VIEW_DOCS: RecoveryPath = {
  id: "view_docs",
  label: "View troubleshooting docs",
  kind: "external",
};
const CONTACT_SUPPORT: RecoveryPath = {
  id: "contact_support",
  label: "Contact support",
  kind: "external",
};

// Maps a backend failure_category (snake_case, may be null) to recovery paths.
// Always returns at least two paths so the failure state offers real choices.
function getRecoveryPaths(failureCategory: string | null): RecoveryPath[] {
  const category = failureCategory?.toLowerCase() ?? "";
  if (/credential|auth|login|password|2fa|verification/.test(category)) {
    return [UPDATE_CREDENTIALS, RETRY];
  }
  if (/element|selector|navigation|not_found|locator|interact/.test(category)) {
    return [EDIT_WORKFLOW, RETRY];
  }
  if (/network|timeout|proxy|rate_limit|connection|throttle/.test(category)) {
    return [RETRY, VIEW_DOCS];
  }
  return [RETRY, EDIT_WORKFLOW, CONTACT_SUPPORT];
}

export { getRecoveryPaths };
export type { RecoveryPath, RecoveryPathId, RecoveryPathKind };
