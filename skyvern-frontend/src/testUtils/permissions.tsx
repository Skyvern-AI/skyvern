import type { ReactNode } from "react";

import {
  PageSlotsProvider,
  type PermissionsResponse,
  type PermissionsState,
} from "@/store/PageSlots";

type OrganizationRole = "admin" | "member";

function permissionsFor(
  role: OrganizationRole,
  capabilities: string[],
): PermissionsResponse {
  return {
    organization_id: "org_test",
    user_id: "user_test",
    auth_kind: "bearer",
    org_role: `org:${role}`,
    role,
    capabilities: capabilities.map((capability) => ({
      capability,
      allowed: role === "admin",
      reason: "role",
    })),
  };
}

function PermissionsTestProvider({
  role,
  capabilities,
  children,
}: {
  role: OrganizationRole;
  capabilities: string[];
  children: ReactNode;
}) {
  return (
    <PermissionsStateTestProvider
      permissions={{
        status: "ready",
        data: permissionsFor(role, capabilities),
      }}
    >
      {children}
    </PermissionsStateTestProvider>
  );
}

function PermissionsStateTestProvider({
  permissions,
  children,
}: {
  permissions: PermissionsState;
  children: ReactNode;
}) {
  return (
    <PageSlotsProvider value={{ permissions }}>{children}</PageSlotsProvider>
  );
}

export { PermissionsStateTestProvider, PermissionsTestProvider };
