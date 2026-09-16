// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

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

const { getClientMock, realRunQuery } = vi.hoisted(() => ({
  getClientMock: vi.fn(),
  // The stale-URL case drives the real query through a seeded cache; the rest
  // only need a payload, so they keep the cheaper stub.
  realRunQuery: { enabled: false },
}));

const mocks = vi.hoisted(() => ({
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

vi.mock("@/routes/runs/useTaskV2Query", () => ({
  useTaskV2Query: () => mocks.taskV2(),
}));
vi.mock("@/api/AxiosClient", () => ({ getClient: getClientMock }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => undefined,
}));
vi.mock(
  "@/routes/workflows/hooks/useWorkflowRunWithWorkflowQuery",
  async (importOriginal) => {
    const actual =
      await importOriginal<
        typeof import("@/routes/workflows/hooks/useWorkflowRunWithWorkflowQuery")
      >();
    return {
      useWorkflowRunWithWorkflowQuery: (options?: {
        workflowRunId: string | undefined;
        enabled?: boolean;
      }) =>
        realRunQuery.enabled
          ? actual.useWorkflowRunWithWorkflowQuery(options)
          : mocks.runQuery(options),
    };
  },
);
// The studio shell is stubbed to a marker that echoes the resolved wpid, so we
// verify both the branch choice and that the provider fed the id through.
vi.mock("@/routes/workflows/editor/WorkflowEditor", () => ({
  WorkflowEditor: () => (
    <div data-testid="studio">studio:{useWorkflowPermanentId()}</div>
  ),
}));

function LocationProbe() {
  const location = useLocation();
  return (
    <div data-testid="location">{location.pathname + location.search}</div>
  );
}

function renderAt(entry: string, client?: QueryClient) {
  const queryClient =
    client ??
    new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
  const tree = (
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/runs/:runId/*" element={<RunRouter />} />
        <Route path="/agents/*" element={<div data-testid="redirected" />} />
      </Routes>
      <LocationProbe />
    </MemoryRouter>
  );
  return render(
    <QueryClientProvider client={queryClient}>{tree}</QueryClientProvider>,
  );
}

function expectCenteredLoadingIndicator() {
  const logo = screen.getByAltText("Minimized Logo");
  const pulse = logo.parentElement;
  const wrapper = pulse?.parentElement;

  expect(screen.getByRole("status").textContent).toContain("Loading");
  expect(pulse?.classList.contains("animate-pulse")).toBe(true);
  expect(wrapper?.classList.contains("flex")).toBe(true);
  expect(wrapper?.classList.contains("h-screen")).toBe(true);
  expect(wrapper?.classList.contains("w-full")).toBe(true);
  expect(wrapper?.classList.contains("items-center")).toBe(true);
  expect(wrapper?.classList.contains("justify-center")).toBe(true);
}

describe("RunRouter", () => {
  beforeEach(() => {
    mocks.taskV2.mockReturnValue({ data: undefined, isLoading: false });
    mocks.runQuery.mockReturnValue({ data: resolvedRun, isLoading: false });
  });

  afterEach(() => {
    realRunQuery.enabled = false;
    getClientMock.mockReset();
  });

  test("studio on: renders the studio in place under /runs/{wr} (no redirect to /agents)", () => {
    renderAt("/runs/wr_1");
    expect(screen.getByTestId("studio").textContent).toBe("studio:wpid_123");
    expect(screen.queryByTestId("redirected")).toBeNull();
  });

  test.each([
    ["overview", "timeline", "overview,browser"],
    ["blocks", "timeline", "overview,browser"],
    ["output", "timeline", "overview,browser"],
    ["parameters", "timeline", "overview,browser"],
    ["recording", "recording", "browser,overview"],
    ["code", "code", "overview,browser"],
  ])("preserves the direct %s subview", (legacySubview, studioView, panes) => {
    renderAt(`/runs/wr_1/${legacySubview}?active=act_1`);

    expect(screen.getByTestId("location").textContent).toBe(
      `/runs/wr_1?active=act_1&view=${studioView}&panes=${panes}`,
    );
  });

  test.each([
    ["output", "outputs"],
    ["parameters", "inputs"],
  ])(
    "preserves the run-level %s subview without an active block",
    (legacySubview, studioView) => {
      renderAt(`/runs/wr_1/${legacySubview}`);

      expect(screen.getByTestId("location").textContent).toBe(
        `/runs/wr_1?view=${studioView}&panes=overview,browser`,
      );
    },
  );

  test("shows the fetching treatment while the workflow run resolves", () => {
    mocks.runQuery.mockReturnValue({ data: undefined, isLoading: true });
    renderAt("/runs/wr_1");
    expectCenteredLoadingIndicator();
    expect(screen.queryByTestId("studio")).toBeNull();
  });

  test("task v2: shows the centered loading indicator while the task resolves", () => {
    mocks.taskV2.mockReturnValue({ data: undefined, isLoading: true });
    renderAt("/runs/tsk_v2_1");
    expectCenteredLoadingIndicator();
    expect(screen.queryByTestId("studio")).toBeNull();
  });

  test("studio on: waits out a stale (keepPreviousData) run from a prior URL", () => {
    realRunQuery.enabled = true;
    getClientMock.mockResolvedValue({ get: () => new Promise(() => {}) });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    client.setQueryData(["workflowRun", "wr_1"], {
      workflow_run_id: "wr_0",
      workflow: { workflow_permanent_id: "wpid_prev" },
    });

    renderAt("/runs/wr_1", client);

    expectCenteredLoadingIndicator();
    expect(screen.queryByTestId("studio")).toBeNull();
  });

  test("enables the run-resolver query for the wr_ id", () => {
    renderAt("/runs/wr_1");
    expect(mocks.runQuery).toHaveBeenCalledWith(
      expect.objectContaining({ workflowRunId: "wr_1", enabled: true }),
    );
  });

  test("does not fetch a workflow run for task routes", () => {
    renderAt("/runs/tsk_1");
    expect(mocks.runQuery).toHaveBeenCalledWith(
      expect.objectContaining({ enabled: false }),
    );
  });

  test("embed=true renders a chrome-free Overview-only studio run", async () => {
    renderAt("/runs/wr_1?embed=true");
    expect(screen.getByTestId("location").textContent).toBe(
      "/runs/wr_1?embed=true&panes=overview",
    );
    await waitFor(() => {
      expect(screen.getByTestId("studio").textContent).toBe("studio:wpid_123");
    });
  });

  test("embedded recording links focus only the Browser pane", () => {
    renderAt("/runs/wr_1/recording?embed=true");
    expect(screen.getByTestId("location").textContent).toBe(
      "/runs/wr_1?embed=true&view=recording&panes=browser",
    );
  });

  test("embedded in-app pane changes are not normalized away", () => {
    renderAt("/runs/wr_1?embed=true&panes=browser&active=wrb_1");

    expect(screen.getByTestId("studio").textContent).toBe("studio:wpid_123");
    expect(screen.getByTestId("location").textContent).toBe(
      "/runs/wr_1?embed=true&panes=browser&active=wrb_1",
    );
  });

  test("embedded URLs cannot open authoring panes", () => {
    renderAt("/runs/wr_1?embed=true&panes=editor,browser");

    expect(screen.getByTestId("location").textContent).toBe(
      "/runs/wr_1?embed=true&panes=overview",
    );
  });

  test("a permanently failed run fetch lands on 404, not an endless spinner", () => {
    mocks.runQuery.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
    });
    renderAt("/runs/wr_1");
    expect(screen.queryByTestId("studio")).toBeNull();
    expect(screen.queryByAltText("Minimized Logo")).toBeNull();
  });

  test("studio on: the studio owns ?wr= — the path is never rewritten under it", () => {
    renderAt("/runs/wr_1?wr=wr_2&panes=editor,overview");
    expect(screen.getByTestId("studio").textContent).toBe("studio:wpid_123");
    expect(screen.getByTestId("location").textContent).toBe(
      "/runs/wr_1?wr=wr_2&panes=editor,overview",
    );
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
    expect(screen.queryByAltText("Minimized Logo")).toBeNull();
  });
});
