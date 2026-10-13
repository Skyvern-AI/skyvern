import { createContext, useContext } from "react";

export type WorkflowAnalyticsPanelProps = {
  workflowPermanentId: string;
};
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
  role: "admin" | "member" | null;
  capabilities: ReadonlyArray<PermissionCapability>;
}>;

export type PermissionsState =
  | Readonly<{ status: "unavailable"; data?: undefined }>
  | Readonly<{ status: "loading"; data?: undefined }>
  | Readonly<{ status: "error"; data?: undefined }>
  | Readonly<{ status: "ready"; data: PermissionsResponse }>;

export type PageSlots = {
  workflowAnalyticsPanel?: React.ComponentType<WorkflowAnalyticsPanelProps>;
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
