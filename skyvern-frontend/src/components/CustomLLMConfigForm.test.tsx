import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CustomLLMConfigForm } from "./CustomLLMConfigForm";

const createCustomLLM = vi.hoisted(() => vi.fn());

vi.mock("@/hooks/useCustomLLMs", () => ({
  useCustomLLMs: () => ({
    customLLMs: [],
    isLoading: false,
    isCreating: false,
    isUpdating: false,
    isDeleting: false,
    createCustomLLM,
    updateCustomLLM: vi.fn(),
    deleteCustomLLM: vi.fn(),
  }),
}));

const originalScrollIntoView = Element.prototype.scrollIntoView;

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  Element.prototype.scrollIntoView = originalScrollIntoView;
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
      render(<CustomLLMConfigForm />);
      fillModel();
      fireEvent.change(screen.getByLabelText("API Base"), {
        target: { value: "https://llm.example.test/v1" },
      });
      fireEvent.change(screen.getByLabelText("API Key (optional)"), {
        target: { value: apiKey },
      });
      fireEvent.click(screen.getByRole("button", { name: "Add Custom LLM" }));

      await waitFor(() => expect(createCustomLLM).toHaveBeenCalledOnce());
      expect(createCustomLLM.mock.calls[0]![0].config).toMatchObject({
        provider: "openai_compatible",
        api_base: "https://llm.example.test/v1",
        api_key: null,
      });
    },
  );

  it("still requires an API base for a keyless endpoint", async () => {
    render(<CustomLLMConfigForm />);
    fillModel();
    fireEvent.click(screen.getByRole("button", { name: "Add Custom LLM" }));

    expect(await screen.findByText("API base is required")).toBeTruthy();
    expect(createCustomLLM).not.toHaveBeenCalled();
  });

  it.each(["OpenRouter", "Gemini"])(
    "still requires an API key for %s",
    async (provider) => {
      render(<CustomLLMConfigForm />);
      fireEvent.keyDown(screen.getByRole("combobox"), { key: "Enter" });
      fireEvent.click(await screen.findByRole("option", { name: provider }));
      fillModel();
      fireEvent.click(screen.getByRole("button", { name: "Add Custom LLM" }));

      expect(await screen.findByText("API key is required")).toBeTruthy();
      expect(createCustomLLM).not.toHaveBeenCalled();
    },
  );
});
