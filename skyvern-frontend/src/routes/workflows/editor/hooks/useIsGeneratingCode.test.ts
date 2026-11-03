// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";

import type { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";
import type { WorkflowRunStatusApiResponse } from "@/api/types";
import * as workflowQueryModule from "@/routes/workflows/hooks/useWorkflowQuery";
import * as blockScriptsQueryModule from "@/routes/workflows/hooks/useBlockScriptsQuery";
import * as workflowRunQueryModule from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useIsGeneratingCode } from "./useIsGeneratingCode";

function mockQueryResult<T>(data: T) {
  return { data } as unknown;
}

vi.mock("@/routes/workflows/hooks/useWorkflowQuery", () => ({
  useWorkflowQuery: vi.fn(),
}));
vi.mock("@/routes/workflows/hooks/useBlockScriptsQuery", () => ({
  useBlockScriptsQuery: vi.fn(),
}));
vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: vi.fn(),
}));

function workflow(
  bodyBlockLabels: string[],
  finallyBlockLabel: string | null = null,
) {
  return {
    workflow_definition: {
      blocks: bodyBlockLabels.map((label) => ({ label })),
      finally_block_label: finallyBlockLabel,
      parameters: [],
    },
  } as unknown as WorkflowApiResponse;
}

function workflowRun(status: WorkflowRunStatusApiResponse["status"]) {
  return {
    status,
  } as unknown as WorkflowRunStatusApiResponse;
}

describe("useIsGeneratingCode", () => {
  const useWorkflowQueryMock = vi.mocked(workflowQueryModule.useWorkflowQuery);
  const useWorkflowRunQueryMock = vi.mocked(
    workflowRunQueryModule.useWorkflowRunQuery,
  );
  const useBlockScriptsQueryMock = vi.mocked(
    blockScriptsQueryModule.useBlockScriptsQuery,
  );

  // Stands in for the hook's own withholding: a caller that states a cleared run
  // gets no payload, while omitting the options object resolves to the route's run.
  function serveRunOnlyToTheRoute() {
    useWorkflowRunQueryMock.mockImplementation(
      (options) =>
        mockQueryResult(
          options ? undefined : workflowRun("running"),
        ) as ReturnType<typeof workflowRunQueryModule.useWorkflowRunQuery>,
    );
  }

  beforeEach(() => {
    vi.clearAllMocks();
    useWorkflowQueryMock.mockReturnValue(
      mockQueryResult(workflow(["body"])) as ReturnType<
        typeof workflowQueryModule.useWorkflowQuery
      >,
    );
    useWorkflowRunQueryMock.mockReturnValue(
      mockQueryResult(workflowRun("completed")) as ReturnType<
        typeof workflowRunQueryModule.useWorkflowRunQuery
      >,
    );
    useBlockScriptsQueryMock.mockReturnValue(
      mockQueryResult(undefined) as ReturnType<
        typeof blockScriptsQueryModule.useBlockScriptsQuery
      >,
    );
  });

  it("returns true only when generated code is still pending", () => {
    useWorkflowRunQueryMock.mockReturnValue(
      mockQueryResult(workflowRun("running")) as ReturnType<
        typeof workflowRunQueryModule.useWorkflowRunQuery
      >,
    );

    const hook = renderHook(() =>
      useIsGeneratingCode({
        cacheKey: "k",
        cacheKeyValue: "v",
        workflowPermanentId: "wf-1",
      }),
    );
    expect(hook.result.current).toBe(true);
  });

  it("returns false when run is finalized", () => {
    const hook = renderHook(() =>
      useIsGeneratingCode({
        cacheKey: "k",
        cacheKeyValue: "v",
        workflowPermanentId: "wf-1",
      }),
    );
    expect(hook.result.current).toBe(false);
  });

  it("returns false when workflow has no executable blocks", () => {
    useWorkflowQueryMock.mockReturnValue(
      mockQueryResult(workflow([], "cleanup")) as ReturnType<
        typeof workflowQueryModule.useWorkflowQuery
      >,
    );
    useWorkflowRunQueryMock.mockReturnValue(
      mockQueryResult(workflowRun("running")) as ReturnType<
        typeof workflowRunQueryModule.useWorkflowRunQuery
      >,
    );

    const hook = renderHook(() =>
      useIsGeneratingCode({
        cacheKey: "k",
        cacheKeyValue: "v",
        workflowPermanentId: "wf-1",
      }),
    );
    expect(hook.result.current).toBe(false);
  });

  it("returns false when any published script exists", () => {
    useBlockScriptsQueryMock.mockReturnValue(
      mockQueryResult({ blocks: { body: "script" } }) as ReturnType<
        typeof blockScriptsQueryModule.useBlockScriptsQuery
      >,
    );
    useWorkflowRunQueryMock.mockReturnValue(
      mockQueryResult(workflowRun("running")) as ReturnType<
        typeof workflowRunQueryModule.useWorkflowRunQuery
      >,
    );

    const hook = renderHook(() =>
      useIsGeneratingCode({
        cacheKey: "k",
        cacheKeyValue: "v",
        workflowPermanentId: "wf-1",
      }),
    );
    expect(hook.result.current).toBe(false);
  });

  it("defers to the route's run when no run id is given", () => {
    serveRunOnlyToTheRoute();
    const hook = renderHook(() =>
      useIsGeneratingCode({
        cacheKey: "k",
        cacheKeyValue: "v",
        workflowPermanentId: "wf-1",
      }),
    );
    expect(hook.result.current).toBe(true);
  });

  it("asks for no run when the caller's run id is cleared", () => {
    serveRunOnlyToTheRoute();
    const hook = renderHook(() =>
      useIsGeneratingCode({
        cacheKey: "k",
        cacheKeyValue: "v",
        workflowPermanentId: "wf-1",
        workflowRunId: undefined,
      }),
    );
    expect(hook.result.current).toBe(false);
  });
});
