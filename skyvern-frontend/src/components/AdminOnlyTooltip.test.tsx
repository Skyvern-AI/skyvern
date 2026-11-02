// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { AdminOnlyTooltip } from "@/components/AdminOnlyTooltip";
import { useHasCapability, type PermissionsState } from "@/store/PageSlots";
import { PermissionsStateTestProvider } from "@/testUtils/permissions";

const capability = "credential.delete";
const reason = "Organization admin access is required for this action.";
const checking = "Checking permissions…";

function GatedControl() {
  const allowed = useHasCapability(capability);

  return (
    <AdminOnlyTooltip allowed={allowed}>
      <button aria-label="Delete credential" disabled={!allowed}>
        Delete
      </button>
    </AdminOnlyTooltip>
  );
}

function renderWithPermissions(permissions: PermissionsState) {
  render(
    <PermissionsStateTestProvider permissions={permissions}>
      <GatedControl />
    </PermissionsStateTestProvider>,
  );
}

afterEach(cleanup);

describe("AdminOnlyTooltip permission messaging", () => {
  it("uses a neutral label while permissions are loading", async () => {
    renderWithPermissions({ status: "loading" });

    expect(
      (
        screen.getByRole("button", {
          name: "Delete credential",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    const group = screen.getByRole("group", { name: checking });
    fireEvent.focus(group);

    expect((await screen.findByRole("tooltip")).textContent).toBe(checking);
    expect(screen.queryByRole("group", { name: reason })).toBeNull();
  });

  it("does not present a denial label when permission loading fails", () => {
    renderWithPermissions({ status: "error" });

    expect(
      (
        screen.getByRole("button", {
          name: "Delete credential",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    expect(screen.queryByRole("group", { name: reason })).toBeNull();
    expect(screen.queryByText(reason)).toBeNull();
  });

  it("explains a denial once capabilities are ready", async () => {
    renderWithPermissions({
      status: "ready",
      data: {
        organization_id: "org_test",
        user_id: "user_test",
        auth_kind: "bearer",
        org_role: "org:member",
        role: "member",
        capabilities: [{ capability, allowed: false, reason: "role" }],
      },
    });

    const group = screen.getByRole("group", { name: reason });
    fireEvent.focus(group);

    expect((await screen.findByRole("tooltip")).textContent).toBe(reason);
  });
});
