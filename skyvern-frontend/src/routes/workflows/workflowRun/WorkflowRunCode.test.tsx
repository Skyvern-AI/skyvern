// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WorkflowRunCode } from "./WorkflowRunCode";

const mocks = vi.hoisted(() => ({
  workflowRun: undefined as unknown,
  isPlaceholderData: false,
}));

vi.mock("@/routes/workflows/hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => ({
    data: mocks.workflowRun,
    isPlaceholderData: mocks.isPlaceholderData,
  }),
}));
vi.mock("@/routes/workflows/hooks/useBlockScriptsQuery", () => ({
  useBlockScriptsQuery: () => ({ data: undefined, isLoading: false }),
}));
vi.mock("@/routes/workflows/hooks/useCacheKeyValuesQuery", () => ({
  useCacheKeyValuesQuery: () => ({ data: undefined }),
}));
vi.mock("@/routes/workflows/hooks/useScriptVersionsQuery", () => ({
  useScriptVersionsQuery: () => ({ data: undefined }),
}));
vi.mock("@/routes/workflows/hooks/useScriptVersionCodeQuery", () => ({
  useScriptVersionCodeQuery: () => ({ data: undefined }),
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => undefined,
}));
vi.mock("@/components/ui/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));
vi.mock("@/api/AxiosClient", () => ({
  getClient: async () => ({ get: vi.fn() }),
}));
vi.mock("@/routes/workflows/components/CodeEditor", () => ({
  CodeEditor: () => null,
}));

function renderCode(props?: { workflowRunId?: string }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <WorkflowRunCode {...props} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  mocks.workflowRun = undefined;
  mocks.isPlaceholderData = false;
});

describe("WorkflowRunCode", () => {
  // A withheld run leaves workflowPermanentId undefined, which disables both
  // block-scripts queries — so every guard in front of the message is open and
  // "no code" would be asserted about a run the component cannot even see.
  it("does not claim a run has no code while its payload is still withheld", () => {
    mocks.isPlaceholderData = true;

    const { container } = renderCode();

    expect(container.textContent).not.toContain("No code has been generated");
  });

  it("does not claim no code in the studio layout while the payload is withheld", () => {
    mocks.isPlaceholderData = true;

    const { container } = renderCode({ workflowRunId: "wr_1" });

    expect(container.textContent).not.toContain("No code has been generated");
  });

  it("states that a resolved run has no code", () => {
    mocks.workflowRun = {
      workflow_run_id: "wr_1",
      status: "completed",
      workflow: {
        workflow_permanent_id: "wpid_1",
        cache_key: "",
        workflow_definition: { blocks: [], parameters: [] },
      },
    };

    const { container } = renderCode();

    expect(container.textContent).toContain("No code has been generated");
  });
});
