// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";

import { RunRouter } from "./RunRouter";

type RunQueryResult = {
  data:
    | { workflow_run_id: string; workflow: { workflow_permanent_id: string } }
    | undefined;
  isLoading: boolean;
  isError?: boolean;
};

const resolvedRun = {
  workflow_run_id: "wr_1",
  workflow: { workflow_permanent_id: "wpid_123" },
};

const mocks = vi.hoisted(() => ({
  studioEnabled: vi.fn(() => true),
  taskV2: vi.fn(() => ({ data: undefined, isLoading: false })),
  runQuery: vi.fn<
    (options?: { workflowRunId?: string; enabled?: boolean }) => RunQueryResult
  >(() => ({
    data: {
      workflow_run_id: "wr_1",
      workflow: { workflow_permanent_id: "wpid_123" },
    },
    isLoading: false,
  })),
}));

vi.mock("@/hooks/useWorkflowStudioEnabled", () => ({
  useWorkflowStudioEnabled: () => mocks.studioEnabled(),
}));
vi.mock("@/routes/runs/useTaskV2Query", () => ({
  useTaskV2Query: () => mocks.taskV2(),
}));
vi.mock("@/routes/workflows/hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: (options?: {
    workflowRunId?: string;
    enabled?: boolean;
  }) => mocks.runQuery(options),
}));
// The studio shell is stubbed to a marker that echoes the resolved wpid, so we
// verify both the branch choice and that the provider fed the id through.
vi.mock("@/routes/workflows/editor/WorkflowEditor", () => ({
  WorkflowEditor: () => (
    <div data-testid="studio">studio:{useWorkflowPermanentId()}</div>
  ),
}));
vi.mock("@/routes/workflows/WorkflowRun", () => ({
  WorkflowRun: () => <div data-testid="legacy">legacy</div>,
}));

function renderAt(entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/runs/:runId/*" element={<RunRouter />} />
        <Route path="/agents/*" element={<div data-testid="redirected" />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("RunRouter workflow-run branch", () => {
  beforeEach(() => {
    mocks.studioEnabled.mockReturnValue(true);
    mocks.taskV2.mockReturnValue({ data: undefined, isLoading: false });
    mocks.runQuery.mockReturnValue({ data: resolvedRun, isLoading: false });
  });

  test("studio on: renders the studio in place under /runs/{wr} (no redirect to /agents)", () => {
    renderAt("/runs/wr_1");
    expect(screen.getByTestId("studio").textContent).toBe("studio:wpid_123");
    expect(screen.queryByTestId("redirected")).toBeNull();
    expect(screen.queryByTestId("legacy")).toBeNull();
  });

  test("studio on: shows the fetching treatment while the run resolves", () => {
    mocks.runQuery.mockReturnValue({ data: undefined, isLoading: true });
    renderAt("/runs/wr_1");
    expect(screen.getByText("Fetching run details...")).toBeTruthy();
    expect(screen.queryByTestId("studio")).toBeNull();
  });

  test("studio on: waits out a stale (keepPreviousData) run from a prior URL", () => {
    // The query still holds the previous run while navigating to wr_1; its
    // workflow id must not be handed to the studio until the fetch catches up.
    mocks.runQuery.mockReturnValue({
      data: {
        workflow_run_id: "wr_0",
        workflow: { workflow_permanent_id: "wpid_prev" },
      },
      isLoading: false,
    });
    renderAt("/runs/wr_1");
    expect(screen.getByText("Fetching run details...")).toBeTruthy();
    expect(screen.queryByTestId("studio")).toBeNull();
  });

  test("studio off: keeps the legacy run view", () => {
    mocks.studioEnabled.mockReturnValue(false);
    renderAt("/runs/wr_1");
    expect(screen.getByTestId("legacy")).toBeTruthy();
    expect(screen.queryByTestId("studio")).toBeNull();
  });

  test("studio on: enables the run-resolver query for the wr_ id", () => {
    renderAt("/runs/wr_1");
    expect(mocks.runQuery).toHaveBeenCalledWith(
      expect.objectContaining({ workflowRunId: "wr_1", enabled: true }),
    );
  });

  test("studio off: disables the run-resolver query so non-studio routes don't fetch a workflow run", () => {
    mocks.studioEnabled.mockReturnValue(false);
    renderAt("/runs/wr_1");
    expect(mocks.runQuery).toHaveBeenCalledWith(
      expect.objectContaining({ enabled: false }),
    );
  });

  test("embed=true keeps the chrome-free legacy view, not the studio shell", () => {
    renderAt("/runs/wr_1?embed=true");
    expect(screen.getByTestId("legacy")).toBeTruthy();
    expect(screen.queryByTestId("studio")).toBeNull();
  });

  test("a permanently failed run fetch lands on 404, not an endless spinner", () => {
    mocks.runQuery.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
    });
    renderAt("/runs/wr_1");
    expect(screen.queryByTestId("studio")).toBeNull();
    expect(screen.queryByText("Fetching run details...")).toBeNull();
    expect(screen.queryByTestId("legacy")).toBeNull();
  });

  test("a failed background poll keeps the studio view while the live run stays retained", () => {
    // isError flips true when a 5s poll of a live run fails, but keepPreviousData
    // still holds the matching run; the resolved run must win over the error so a
    // single failed poll never flashes 404 over a working studio view.
    mocks.runQuery.mockReturnValue({
      data: resolvedRun,
      isLoading: false,
      isError: true,
    });
    renderAt("/runs/wr_1");
    expect(screen.getByTestId("studio").textContent).toBe("studio:wpid_123");
    expect(screen.queryByText("Fetching run details...")).toBeNull();
  });
});
