// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CustomLLM } from "@/api/types";
import { PermissionsTestProvider } from "@/testUtils/permissions";
import { CustomLLMConfigForm } from "./CustomLLMConfigForm";

Object.defineProperty(globalThis, "ResizeObserver", {
  value: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
});

const state = vi.hoisted(() => ({
  customLLMs: [] as unknown[],
  isLoading: false,
  createCustomLLM: vi.fn(),
  updateCustomLLM: vi.fn(),
  deleteCustomLLM: vi.fn(),
  isCreating: false,
  isUpdating: false,
  isDeleting: false,
}));

vi.mock("@/hooks/useCustomLLMs", () => ({
  useCustomLLMs: () => state,
}));

const capabilities = [
  "custom_model.create",
  "custom_model.update",
  "custom_model.delete",
];
const reason = "Organization admin access is required for this action.";
const customLLM: CustomLLM = {
  id: "model_1",
  organization_id: "org_test",
  created_at: "2026-01-01T00:00:00Z",
  modified_at: "2026-01-01T00:00:00Z",
  valid: true,
  config: {
    display_name: "Local Llama",
    provider: "ollama",
    model_name: "llama3.1",
    api_base: "http://localhost:11434",
    supports_vision: false,
    add_assistant_prefix: false,
  },
};

function renderFor(role: "admin" | "member") {
  render(
    <PermissionsTestProvider role={role} capabilities={capabilities}>
      <CustomLLMConfigForm />
    </PermissionsTestProvider>,
  );
}

async function expectTooltipFor(button: HTMLElement) {
  const wrapper = button.closest<HTMLElement>("[role='group'][tabindex='0']");
  expect(wrapper).not.toBeNull();
  fireEvent.focus(wrapper!);
  expect((await screen.findByRole("tooltip")).textContent).toBe(reason);
}

beforeEach(() => {
  state.customLLMs = [customLLM];
  state.isLoading = false;
  state.isCreating = false;
  state.isUpdating = false;
  state.isDeleting = false;
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("CustomLLMConfigForm capabilities", () => {
  it("disables create, update, and delete for a Member with an explanation", async () => {
    renderFor("member");

    const createButton = screen.getByRole("button", { name: "Add Custom LLM" });
    const updateButton = screen.getByRole("button", { name: "Edit" });
    const deleteButton = screen.getByRole("button", { name: "Delete" });
    expect((createButton as HTMLButtonElement).disabled).toBe(true);
    expect((updateButton as HTMLButtonElement).disabled).toBe(true);
    expect((deleteButton as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByLabelText("Name") as HTMLInputElement).disabled).toBe(
      true,
    );

    await expectTooltipFor(createButton);
    fireEvent.blur(createButton.closest("[role='group'][tabindex='0']")!);
    await waitFor(() => expect(screen.queryByRole("tooltip")).toBeNull());
    await expectTooltipFor(updateButton);
    fireEvent.blur(updateButton.closest("[role='group'][tabindex='0']")!);
    await waitFor(() => expect(screen.queryByRole("tooltip")).toBeNull());
    await expectTooltipFor(deleteButton);
  });

  it("keeps all custom model actions enabled for an Admin", () => {
    renderFor("admin");

    expect(
      (
        screen.getByRole("button", {
          name: "Add Custom LLM",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
    expect(
      (screen.getByRole("button", { name: "Edit" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
    expect(
      (screen.getByRole("button", { name: "Delete" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(
      (
        screen.getByRole("button", {
          name: "Save Custom LLM",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
  });
});
