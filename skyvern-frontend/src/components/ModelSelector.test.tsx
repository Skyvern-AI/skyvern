// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { WorkflowScopeContext } from "@/routes/workflows/editor/WorkflowScopeContext";

import { ModelSelector } from "./ModelSelector";

const getMock = vi
  .fn()
  .mockResolvedValue({ data: { models: { "gpt-4o": "GPT-4o" } } });

vi.mock("@/api/AxiosClient", () => ({
  getClient: () => Promise.resolve({ get: getMock }),
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("@/components/ui/select", () => ({
  Select: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  SelectContent: ({ children }: { children?: ReactNode }) => (
    <div>{children}</div>
  ),
  SelectItem: ({
    children,
    value,
  }: {
    children?: ReactNode;
    value: string;
  }) => <div data-testid={`select-item-${value}`}>{children}</div>,
  SelectTrigger: ({ children }: { children?: ReactNode }) => (
    <button type="button">{children}</button>
  ),
  SelectValue: ({ placeholder }: { placeholder?: string }) => (
    <span>{placeholder}</span>
  ),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderSelector(
  readOnly: boolean,
  value: { model_name: string } | null = { model_name: "removed-model-x" },
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <WorkflowScopeContext.Provider value={{ workflowId: "w", readOnly }}>
        <ModelSelector value={value} onChange={() => {}} />
      </WorkflowScopeContext.Provider>
    </QueryClientProvider>,
  );
}

describe("ModelSelector in a read-only comparison scope", () => {
  test("shows the stored model verbatim and does not fetch /models", () => {
    renderSelector(true);
    expect(screen.getByTestId("model-selector-readonly").textContent).toBe(
      "removed-model-x",
    );
    expect(getMock).not.toHaveBeenCalled();
  });

  test("renders the interactive selector in the live editor scope", () => {
    renderSelector(false);
    expect(screen.queryByTestId("model-selector-readonly")).toBeNull();
  });

  test("marks Claude Opus models as enterprise-only", async () => {
    getMock.mockResolvedValueOnce({
      data: {
        models: {
          "us.anthropic.claude-opus-4-20250514-v1:0": "Anthropic Claude 4 Opus",
          "claude-opus-4-6": "Anthropic Claude 4.6 Opus",
          "claude-opus-4-7": "Anthropic Claude 4.7 Opus",
          "claude-opus-4-8": "Anthropic Claude 4.8 Opus",
        },
      },
    });

    renderSelector(false, null);

    expect(await screen.findByText("Anthropic Claude 4 Opus")).toBeTruthy();
    expect(await screen.findByText("Anthropic Claude 4.6 Opus")).toBeTruthy();
    expect(await screen.findByText("Anthropic Claude 4.7 Opus")).toBeTruthy();
    expect(await screen.findByText("Anthropic Claude 4.8 Opus")).toBeTruthy();
    expect(screen.getAllByText("Enterprise")).toHaveLength(4);
  });

  test("marks selected deprecated Claude Opus 4.5 as enterprise-only", async () => {
    getMock.mockResolvedValueOnce({
      data: {
        models: {
          "claude-opus-4-5-20251101": "Anthropic Claude 4.5 Opus",
        },
      },
    });

    renderSelector(false, { model_name: "claude-opus-4-5-20251101" });

    expect(
      await screen.findByText("Anthropic Claude 4.5 Opus (deprecated)"),
    ).toBeTruthy();
    expect(screen.getByText("Enterprise")).toBeTruthy();
  });

  test("hides newly deprecated models unless currently selected", async () => {
    getMock.mockResolvedValueOnce({
      data: {
        models: {
          "azure/gpt-5.2": "GPT 5.2",
          "claude-opus-4-5-20251101": "Anthropic Claude 4.5 Opus",
          "mercury-2": "Inception Mercury 2",
          "azure/gpt-5.4": "GPT 5.4",
        },
      },
    });

    renderSelector(false, null);

    expect(await screen.findByText("GPT 5.4")).toBeTruthy();
    expect(screen.queryByText("GPT 5.2 (deprecated)")).toBeNull();
    expect(
      screen.queryByText("Anthropic Claude 4.5 Opus (deprecated)"),
    ).toBeNull();
    expect(screen.queryByText("Inception Mercury 2 (deprecated)")).toBeNull();
  });

  test("keeps a selected deprecated model visible", async () => {
    getMock.mockResolvedValueOnce({
      data: {
        models: {
          "azure/gpt-5.2": "GPT 5.2",
          "azure/gpt-5.4": "GPT 5.4",
        },
      },
    });

    renderSelector(false, { model_name: "azure/gpt-5.2" });

    expect(await screen.findByText("GPT 5.2 (deprecated)")).toBeTruthy();
  });

  test("hides deprecated Gemini 2.5 Pro/Flash unless selected", async () => {
    getMock.mockResolvedValueOnce({
      data: {
        models: {
          "gemini-2.5-pro-preview-05-06": "Gemini 2.5 Pro",
          "gemini-2.5-flash": "Gemini 2.5 Flash",
          "gemini-3.5-flash": "Gemini 3.5 Flash",
        },
      },
    });

    renderSelector(false, null);

    expect(await screen.findByText("Gemini 3.5 Flash")).toBeTruthy();
    expect(screen.queryByText("Gemini 2.5 Pro (deprecated)")).toBeNull();
    expect(screen.queryByText("Gemini 2.5 Flash (deprecated)")).toBeNull();
  });

  test("surfaces the open-source OpenRouter models", async () => {
    getMock.mockResolvedValueOnce({
      data: {
        models: {
          "deepseek-v4-flash": "DeepSeek V4 Flash",
          "mimo-v2.5": "Xiaomi MiMo V2.5",
        },
      },
    });

    renderSelector(false, null);

    expect(await screen.findByText("DeepSeek V4 Flash")).toBeTruthy();
    expect(screen.getByText("Xiaomi MiMo V2.5")).toBeTruthy();
  });

  test("surfaces the new Gemini 3.5 Flash Lite and 3.6 Flash models", async () => {
    getMock.mockResolvedValueOnce({
      data: {
        models: {
          "gemini-3.5-flash-lite": "Gemini 3.5 Flash Lite",
          "gemini-3.6-flash": "Gemini 3.6 Flash",
        },
      },
    });

    renderSelector(false, null);

    expect(await screen.findByText("Gemini 3.5 Flash Lite")).toBeTruthy();
    expect(screen.getByText("Gemini 3.6 Flash")).toBeTruthy();
  });
});
