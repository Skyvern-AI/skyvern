// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { OrganizationMembers } from "./OrganizationMembers";
import {
  OidcAuthContext,
  type OidcAuthContextValue,
} from "@/store/OnPremOidcContext";
import { PageSlotsProvider, type PermissionsState } from "@/store/PageSlots";

const mocks = vi.hoisted(() => ({ get: vi.fn(), getClient: vi.fn() }));

vi.mock("@/api/AxiosClient", () => ({
  getClient: mocks.getClient,
}));

const managerPermissions: PermissionsState = {
  status: "ready",
  data: {
    organization_id: "org-test",
    user_id: "user-admin",
    auth_kind: "bearer",
    org_role: "org:admin",
    role: "admin",
    capabilities: [
      { capability: "organization.rbac.manage", allowed: true, reason: "role" },
    ],
  },
};

const memberPermissions: PermissionsState = {
  status: "ready",
  data: {
    ...managerPermissions.data,
    capabilities: [
      {
        capability: "organization.rbac.manage",
        allowed: false,
        reason: "role",
      },
    ],
  },
};

const lastSeenAt = "2026-10-23T14:30:00";

function oidcContext(enabled: boolean): OidcAuthContextValue {
  return {
    enabled,
    manager: null,
    user: null,
    signIn: async () => undefined,
    signOut: async () => undefined,
    completeSignIn: () => undefined,
  };
}

function renderMembers({
  enabled = true,
  permissions = managerPermissions,
}: {
  enabled?: boolean;
  permissions?: PermissionsState;
} = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  return render(
    <QueryClientProvider client={client}>
      <OidcAuthContext.Provider value={oidcContext(enabled)}>
        <PageSlotsProvider value={{ permissions }}>
          <OrganizationMembers />
        </PageSlotsProvider>
      </OidcAuthContext.Provider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mocks.getClient.mockResolvedValue({ get: mocks.get });
  mocks.get.mockResolvedValue({
    data: {
      enabled: true,
      eligible: true,
      enforce: true,
      suspended_at: null,
      roles: ["admin"],
      members: [
        {
          user_id: "user-1",
          clerk_role: null,
          app_role: "admin",
          effective_role: "admin",
          can_manage_billing: null,
          last_seen_at: lastSeenAt,
        },
        {
          user_id: "user-2",
          clerk_role: null,
          app_role: null,
          effective_role: null,
          can_manage_billing: null,
          last_seen_at: null,
        },
        {
          user_id: "user-3",
          clerk_role: null,
          app_role: "custom_operator",
          effective_role: "custom_operator",
          can_manage_billing: null,
          last_seen_at: null,
        },
      ],
    },
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.clearAllMocks();
});

describe("OrganizationMembers", () => {
  it("renders members with their effective role and last sign-in", async () => {
    vi.stubEnv("TZ", "America/Los_Angeles");
    renderMembers();

    expect(await screen.findByText("user-1")).toBeTruthy();
    expect(screen.getByText("user-2")).toBeTruthy();
    expect(screen.getByText("user-3")).toBeTruthy();
    expect(screen.getByText("Admin")).toBeTruthy();
    expect(screen.getByText("Not available")).toBeTruthy();
    expect(screen.getByText("Custom Operator")).toBeTruthy();
    expect(
      screen.getByText(new Date(`${lastSeenAt}Z`).toLocaleString()),
    ).toBeTruthy();
    expect(
      screen.getByText("Roles are assigned by your identity provider."),
    ).toBeTruthy();
    expect(screen.queryByText("Role source")).toBeNull();
    expect(mocks.getClient).toHaveBeenCalledWith(null, "sans-api-v1");
    expect(mocks.get).toHaveBeenCalledWith("/organizations/rbac");
  });

  it("is hidden when the user cannot manage the organization", () => {
    renderMembers({ permissions: memberPermissions });

    expect(screen.queryByRole("heading", { name: "Members" })).toBeNull();
    expect(mocks.get).not.toHaveBeenCalled();
  });

  it("is hidden when no OIDC issuer is configured", () => {
    renderMembers({ enabled: false });

    expect(screen.queryByRole("heading", { name: "Members" })).toBeNull();
    expect(mocks.get).not.toHaveBeenCalled();
  });

  it("does not render a member list or crash when the API returns 403", async () => {
    mocks.get.mockRejectedValueOnce(
      Object.assign(new Error("Forbidden"), { response: { status: 403 } }),
    );
    renderMembers();

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByText("user-1")).toBeNull();
  });
});
