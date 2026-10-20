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

const originalScrollIntoView = Element.prototype.scrollIntoView;

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
  state.customLLMs = [customLLM];
  state.isLoading = false;
  state.isCreating = false;
  state.isUpdating = false;
  state.isDeleting = false;
});

afterEach(() => {
  cleanup();
  Element.prototype.scrollIntoView = originalScrollIntoView;
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

function fillModel() {
  fireEvent.change(screen.getByLabelText("Name"), {
    target: { value: "Custom endpoint" },
  });
  fireEvent.change(screen.getByLabelText("Model ID"), {
    target: { value: "example-model" },
  });
}

describe("CustomLLMConfigForm", () => {
  it.each(["", "  "])(
    "saves a keyless OpenAI-compatible endpoint with key %j",
    async (apiKey) => {
      renderFor("admin");
      fillModel();
      fireEvent.change(screen.getByLabelText("API Base"), {
        target: { value: "https://llm.example.test/v1" },
      });
      fireEvent.change(screen.getByLabelText("API Key (optional)"), {
        target: { value: apiKey },
      });
      fireEvent.click(screen.getByRole("button", { name: "Add Custom LLM" }));

      await waitFor(() => expect(state.createCustomLLM).toHaveBeenCalledOnce());
      expect(state.createCustomLLM.mock.calls[0]![0].config).toMatchObject({
        provider: "openai_compatible",
        api_base: "https://llm.example.test/v1",
        api_key: null,
      });
    },
  );

  it("still requires an API base for a keyless endpoint", async () => {
    renderFor("admin");
    fillModel();
    fireEvent.click(screen.getByRole("button", { name: "Add Custom LLM" }));

    expect(await screen.findByText("API base is required")).toBeTruthy();
    expect(state.createCustomLLM).not.toHaveBeenCalled();
  });

  it.each(["OpenRouter", "Gemini"])(
    "still requires an API key for %s",
    async (provider) => {
      renderFor("admin");
      fireEvent.keyDown(screen.getByRole("combobox"), { key: "Enter" });
      fireEvent.click(await screen.findByRole("option", { name: provider }));
      fillModel();
      fireEvent.click(screen.getByRole("button", { name: "Add Custom LLM" }));

      expect(await screen.findByText("API key is required")).toBeTruthy();
      expect(state.createCustomLLM).not.toHaveBeenCalled();
    },
  );
});
