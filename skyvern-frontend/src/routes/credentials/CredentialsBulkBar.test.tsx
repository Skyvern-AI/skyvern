// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CredentialApiResponse } from "@/api/types";
import { PermissionsTestProvider } from "@/testUtils/permissions";
import { CredentialsBulkBar } from "./CredentialsBulkBar";

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("./CredentialFolderSelector", () => ({
  CredentialFolderSelector: () => null,
}));

const capability = "credential.delete";
const reason = "Organization admin access is required for this action.";
const credentials = [
  {
    credential_id: "cred_1",
    credential_type: "secret",
    name: "Example credential",
  } as CredentialApiResponse,
];

function renderFor(role: "admin" | "member") {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <PermissionsTestProvider role={role} capabilities={[capability]}>
        <CredentialsBulkBar
          selectedCredentials={credentials}
          isOperating={false}
          onOperatingChange={vi.fn()}
          onClear={vi.fn()}
          onReplaceSelection={vi.fn()}
        />
      </PermissionsTestProvider>
    </QueryClientProvider>,
  );
}

afterEach(cleanup);

describe("CredentialsBulkBar deletion capability", () => {
  it("disables bulk deletion for a Member and explains why on focus", async () => {
    renderFor("member");

    const button = screen.getByRole("button", { name: "Delete" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    const wrapper = button.closest<HTMLElement>("[role='group'][tabindex='0']");
    expect(wrapper).not.toBeNull();
    fireEvent.focus(wrapper!);
    expect((await screen.findByRole("tooltip")).textContent).toBe(reason);
  });

  it("keeps bulk deletion enabled for an Admin", () => {
    renderFor("admin");

    expect(
      (screen.getByRole("button", { name: "Delete" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });
});
