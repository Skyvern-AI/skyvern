import { createContext, useContext } from "react";
import type { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";

export type WorkflowAnalyticsPanelProps = {
  workflowPermanentId: string;
  workflow?: WorkflowApiResponse;
  workflowIsLoading?: boolean;
};
export type WorkflowTriggersPanelProps = Readonly<{
  workflowPermanentId: string;
  onClose: () => void;
}>;
export type WorkflowRunMilestoneCardProps = Readonly<{
  workflowRunId: string;
  rerun?: Readonly<{ to: string; state?: unknown }>;
}>;

export type FirstRunWaitCardProps = Readonly<{
  phase: "run_provisioning" | "debug_browser_warming";
}>;

export type PermissionCapability = Readonly<{
  capability: string;
  allowed: boolean;
  reason: "role";
}>;

export type PermissionsResponse = Readonly<{
  organization_id: string;
  user_id: string | null;
  auth_kind: string | null;
  org_role: string | null;
  role: "admin" | "member" | "billing_admin" | null;
  capabilities: ReadonlyArray<PermissionCapability>;
}>;

export type PermissionsState =
  | Readonly<{
      status: "unavailable";
      data?: undefined;
      personalWorkspaceOwner?: true;
      // The backend has no /auth/capabilities route, so it predates RBAC and enforces nothing.
      endpointMissing?: true;
    }>
  | Readonly<{ status: "loading"; data?: undefined }>
  | Readonly<{ status: "error"; data?: undefined }>
  | Readonly<{ status: "ready"; data: PermissionsResponse }>;

export type PageSlots = {
  workflowAnalyticsPanel?: React.ComponentType<WorkflowAnalyticsPanelProps>;
  workflowTriggersPanel?: React.ComponentType<WorkflowTriggersPanelProps>;
  workflowRunsFilterControls?: React.ComponentType;
  workflowRunMilestoneCard?: React.ComponentType<WorkflowRunMilestoneCardProps>;
  firstRunWaitCard?: React.ComponentType<FirstRunWaitCardProps>;
  workflowCreatorDirectory?: React.ComponentType<{
    children: React.ReactNode;
  }>;
  permissions?: PermissionsState;
};

const PageSlotsContext = createContext<PageSlots>({});

export const PageSlotsProvider = PageSlotsContext.Provider;

export function usePageSlots(): PageSlots {
  return useContext(PageSlotsContext);
}

export function isCapabilityAllowed(
  permissions: PermissionsState | undefined,
  capability: string,
): boolean {
  // Missing page slots are the OSS context; Cloud always provides a permission state.
  if (!permissions) return true;
  if (
    permissions.status === "unavailable" &&
    (permissions.personalWorkspaceOwner || permissions.endpointMissing)
  ) {
    return true;
  }
  return (
    permissions.status === "ready" &&
    permissions.data.capabilities.some(
      (item) => item.capability === capability && item.allowed,
    )
  );
}

export function useHasCapability(capability: string): boolean {
  const { permissions } = usePageSlots();
  return isCapabilityAllowed(permissions, capability);
}
