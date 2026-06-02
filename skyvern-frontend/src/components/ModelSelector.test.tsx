// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
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

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderSelector(readOnly: boolean) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <WorkflowScopeContext.Provider value={{ workflowId: "w", readOnly }}>
        <ModelSelector
          value={{ model_name: "removed-model-x" }}
          onChange={() => {}}
        />
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
});
