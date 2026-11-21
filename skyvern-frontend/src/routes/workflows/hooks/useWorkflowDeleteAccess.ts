import {
  isCapabilityAllowed,
  usePageSlots,
  type PermissionsState,
} from "@/store/PageSlots";

const DELETE_OWN_AGENTS_ONLY_REASON = "You can only delete agents you created.";
const NO_DELETE_GRANT_REASON = "Your role cannot delete agents.";
const BULK_NOT_ALL_OWN_REASON =
  "You can only delete agents you created. Deselect the others to delete the rest.";

// A null originalCreatedBy is an agent whose first version no user created (an API key); the server refuses
// a Builder's delete of it. Undefined means the response did not carry the creator.
export function canDeleteWorkflow(
  permissions: PermissionsState | undefined,
  originalCreatedBy: string | null | undefined,
): boolean {
  if (isCapabilityAllowed(permissions, "workflow.delete")) return true;
  // Unknown permissions must not disable a delete the server may allow; the server enforces.
  if (permissions?.status !== "ready") return true;
  const { capabilities, user_id } = permissions.data;
  // A backend that does not report workflow.delete cannot tell "any agent" from "own agents"; the server enforces.
  if (!capabilities.some((item) => item.capability === "workflow.delete")) {
    return true;
  }
  if (!isCapabilityAllowed(permissions, "workflow.delete.own")) return false;
  // Without the creator the server's ownership check decides, so do not disable a delete that may succeed.
  if (originalCreatedBy === undefined) return true;
  return user_id !== null && originalCreatedBy === user_id;
}

export function useWorkflowDeleteAccess() {
  const { permissions } = usePageSlots();
  const canDeleteOwn = isCapabilityAllowed(permissions, "workflow.delete.own");
  return {
    canDelete: (originalCreatedBy: string | null | undefined) =>
      canDeleteWorkflow(permissions, originalCreatedBy),
    deniedReason: canDeleteOwn
      ? DELETE_OWN_AGENTS_ONLY_REASON
      : NO_DELETE_GRANT_REASON,
    bulkDeniedReason: canDeleteOwn
      ? BULK_NOT_ALL_OWN_REASON
      : NO_DELETE_GRANT_REASON,
  };
}
