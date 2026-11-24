import type { CreatedTrigger } from "@/routes/workflows/ember/narrativeState";
import type { WorkflowReliability } from "@/routes/workflows/types/reliabilityTypes";
import { createContext, useContext } from "react";
import type { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";

export type WorkflowAnalyticsPanelProps = {
  workflowPermanentId: string;
  workflow?: WorkflowApiResponse;
  workflowIsLoading?: boolean;
};
export type WorkflowTriggersPanelProps = Readonly<{
  workflowPermanentId: string;
  // Opens this trigger's detail with its on switch highlighted.
  focusTriggerId?: string;
  // Changes on every request to focus, so asking again for the same trigger reopens it.
  focusRequest?: number;
}>;
export type AgentStartsBadgeProps = Readonly<{ workflowPermanentId: string }>;
export type TriggerCreatedCardSlotProps = Readonly<{
  trigger: CreatedTrigger;
  onOpen?: () => void;
}>;
export type WorkflowRunMilestoneCardProps = Readonly<{
  workflowRunId: string;
  rerun?: Readonly<{ to: string; state?: unknown }>;
}>;

export type FirstRunWaitCardProps = Readonly<{
  phase: "run_provisioning" | "debug_browser_warming";
}>;

type BrokerCredentialSelectorBase = Readonly<{
  credentialSource: "native" | "nango";
  connectionId: string;
  disabled: boolean;
  onChange: (source: "native" | "nango", id: string) => void;
}>;

export type BrokerCredentialSelectorProps = BrokerCredentialSelectorBase &
  (
    | Readonly<{ provider: "powerbi"; nodeId?: string }>
    | Readonly<{
        provider: "gmail";
        nodeId: string;
        credentialId: string;
        // send filters the native accounts to those that can send; reply also omits connected accounts.
        purpose?: "read" | "send" | "reply";
        // false hides "Use template expression" where a real account is required.
        allowTemplates?: boolean;
      }>
  );

// The Integrations page's Connected Gmail flow, for a surface that starts a new Gmail connection.
export type ConnectedGmailFlow = Readonly<{
  // "default": no visible native Gmail credential, so a new Gmail connection starts Connected Gmail.
  status: "loading" | "default" | "unavailable";
  canConnect: boolean;
  busy: boolean;
  // A message to show for a failed attempt; a cancelled sign-in has none.
  failure: string | null;
  // Active connections: the user's name for one, or the Connected Gmail name, beside its masked account.
  connections: ReadonlyArray<
    Readonly<{
      connection_id: string;
      name: string;
      email_address?: string | null;
    }>
  >;
  // The connection this flow's own sign-in finished with; one added anywhere else never appears here.
  completedConnectionId?: string | null;
  start: () => void;
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
  // When the organization enforces app roles, effective_role overrides the Clerk role above.
  rbac?: Readonly<{ enforce: boolean; effective_role: string | null }>;
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
  powerBIDatasetPicker?: React.ComponentType<
    Readonly<{
      credentialSource?: "native" | "nango";
      connectionId: string;
      value: string;
      disabled: boolean;
      onChange: (value: string) => void;
    }>
  >;
  brokerCredentialSelector?: React.ComponentType<BrokerCredentialSelectorProps>;
  powerBIDaxAssist?: React.ComponentType<
    Readonly<{
      credentialSource: "native" | "nango" | undefined;
      connectionId: string;
      datasetId: string;
      currentQuery: string;
      maxRows: number;
      disabled: boolean;
      // False when the editor refused the change, so the assist keeps its suggestion.
      onUse: (query: string) => boolean;
    }>
  >;
  connectedGmailFlow?: React.ComponentType<
    Readonly<{ children: (flow: ConnectedGmailFlow) => React.ReactNode }>
  >;
  workflowFindingsScope?: React.ComponentType<
    Readonly<{
      workflowPermanentIds: readonly string[];
      children: React.ReactNode;
    }>
  >;
  workflowFindingsTrigger?: React.ComponentType<
    Readonly<{
      workflowPermanentId: string;
      title?: string;
      surface: "list" | "editor";
      reliability?: WorkflowReliability;
    }>
  >;
  workflowCuratorSettings?: React.ComponentType<
    Readonly<{ workflowPermanentId: string; readOnly: boolean }>
  >;
  workflowAnalyticsPanel?: React.ComponentType<WorkflowAnalyticsPanelProps>;
  workflowTriggersPanel?: React.ComponentType<WorkflowTriggersPanelProps>;
  // How an agent starts (triggers, schedule, manual), on the Agents list.
  agentStartsBadge?: React.ComponentType<AgentStartsBadgeProps>;
  // The Ember trigger card with the trigger's live state and a Turn on switch.
  triggerCreatedCard?: React.ComponentType<TriggerCreatedCardSlotProps>;
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
