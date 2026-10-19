// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getClient } from "@/api/AxiosClient";
import type { CredentialApiResponse } from "@/api/types";
import { PermissionsTestProvider } from "@/testUtils/permissions";
import { DeleteCredentialButton } from "./DeleteCredentialButton";

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

const mockedGetClient = vi.mocked(getClient);
const capability = "credential.delete";
const reason = "Organization admin access is required for this action.";
const credential = {
  credential_id: "cred_1",
  credential_type: "secret",
  name: "Example credential",
} as CredentialApiResponse;

function renderFor(role: "admin" | "member") {
  const queryClient = new QueryClient();
  render(
    <QueryClientProvider client={queryClient}>
      <PermissionsTestProvider role={role} capabilities={[capability]}>
        <DeleteCredentialButton credential={credential} />
      </PermissionsTestProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("DeleteCredentialButton capability", () => {
  it("disables deletion for a Member and explains why on keyboard focus", async () => {
    renderFor("member");

    const button = screen.getByRole("button", { name: "Delete credential" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    const wrapper = button.closest<HTMLElement>("[role='group'][tabindex='0']");
    expect(wrapper).not.toBeNull();
    fireEvent.focus(wrapper!);
    expect((await screen.findByRole("tooltip")).textContent).toBe(reason);
    expect(mockedGetClient).not.toHaveBeenCalled();
  });

  it("keeps deletion enabled for an Admin", () => {
    renderFor("admin");

    const button = screen.getByRole("button", { name: "Delete credential" });
    expect((button as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(button);
    expect(
      screen.getByRole("heading", { name: "Delete credential?" }),
    ).toBeTruthy();
  });
});
