// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getClient } from "@/api/AxiosClient";
import type { CustomLLM, OrganizationApiResponse } from "@/api/types";
import { PermissionsTestProvider } from "@/testUtils/permissions";
import { OrgLLMDefaultsCard } from "./OrgLLMDefaultsCard";

Object.defineProperty(Element.prototype, "scrollIntoView", {
  value: () => {},
});

const state = vi.hoisted(() => ({ customLLMs: [] as unknown[] }));

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("@/hooks/useCustomLLMs", () => ({
  useCustomLLMs: () => ({ customLLMs: state.customLLMs, isLoading: false }),
}));

const capability = "organization.settings.update";
const reason = "Organization admin access is required for this action.";
const model: CustomLLM = {
  id: "model_1",
  organization_id: "org_test",
  created_at: "2026-01-01T00:00:00Z",
  modified_at: "2026-01-01T00:00:00Z",
  valid: true,
  config: {
    display_name: "Local Llama",
    provider: "ollama",
    model_name: "llama3.1",
    supports_vision: false,
    add_assistant_prefix: false,
  },
};
const organization = {
  organization_id: "org_test",
  organization_name: "Example organization",
  created_at: "2026-01-01T00:00:00Z",
  modified_at: "2026-01-01T00:00:00Z",
  max_retries_per_step: 3,
  max_steps_per_run: 10,
  webhook_callback_url: null,
} as OrganizationApiResponse;

function renderFor(role: "admin" | "member") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <PermissionsTestProvider role={role} capabilities={[capability]}>
        <OrgLLMDefaultsCard />
      </PermissionsTestProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  state.customLLMs = [model];
  vi.mocked(getClient).mockResolvedValue({
    get: vi.fn().mockResolvedValue({ data: { organizations: [organization] } }),
    put: vi.fn().mockResolvedValue({}),
  } as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("OrgLLMDefaultsCard capability", () => {
  it("disables both defaults for a Member and explains the restriction", async () => {
    renderFor("member");

    const smartSelect = await screen.findByRole("combobox", {
      name: "Smart LLM",
    });
    const fastSelect = screen.getByRole("combobox", { name: "Fast LLM" });
    expect((smartSelect as HTMLButtonElement).disabled).toBe(true);
    expect((fastSelect as HTMLButtonElement).disabled).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Save" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    const wrapper = smartSelect.closest<HTMLElement>(
      "[role='group'][tabindex='0']",
    );
    expect(wrapper).not.toBeNull();
    fireEvent.focus(wrapper!);
    expect((await screen.findByRole("tooltip")).textContent).toBe(reason);
  });

  it("lets an Admin change and save organization defaults", async () => {
    renderFor("admin");

    const smartSelect = await screen.findByRole("combobox", {
      name: "Smart LLM",
    });
    await waitFor(() => {
      expect((smartSelect as HTMLButtonElement).disabled).toBe(false);
    });
    fireEvent.click(smartSelect);
    fireEvent.click(await screen.findByRole("option", { name: "Local Llama" }));

    await waitFor(() => {
      expect(
        (screen.getByRole("button", { name: "Save" }) as HTMLButtonElement)
          .disabled,
      ).toBe(false);
    });
  });
});
