// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
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
  studioFlagState: vi.fn<() => boolean | undefined>(() => true),
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
  useWorkflowStudioFlagState: () => mocks.studioFlagState(),
  useWorkflowStudioEnabled: () => mocks.studioFlagState() ?? false,
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
vi.mock("@/routes/workflows/WorkflowRun", () => ({
  WorkflowRun: () => <div data-testid="legacy">legacy</div>,
}));

function LocationProbe() {
  const location = useLocation();
  return (
    <div data-testid="location">{location.pathname + location.search}</div>
  );
}

function renderAt(entry: string, client?: QueryClient) {
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
    client ? (
      <QueryClientProvider client={client}>{tree}</QueryClientProvider>
    ) : (
      tree
    ),
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
    mocks.studioFlagState.mockReturnValue(true);
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
    expect(screen.queryByTestId("legacy")).toBeNull();
  });

  test("studio on: shows the fetching treatment while the run resolves", () => {
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

  test("studio off: keeps the legacy run view", () => {
    mocks.studioFlagState.mockReturnValue(false);
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
    mocks.studioFlagState.mockReturnValue(false);
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

  test("embed=true honors ?wr= even while the studio flag is unresolved", () => {
    // Embed never renders the studio shell, so there is no URL state to protect
    // and the redirect need not wait for the flag.
    mocks.studioFlagState.mockReturnValue(undefined);
    renderAt("/runs/wr_1?embed=true&wr=wr_2&active=act_9");
    expect(screen.getByTestId("location").textContent).toBe(
      "/runs/wr_2/overview?embed=true&active=act_9",
    );
    expect(screen.getByTestId("legacy")).toBeTruthy();
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
    expect(screen.queryByTestId("legacy")).toBeNull();
  });

  test("studio off: a studio-shared link resolves to the ?wr= run, selection intact", () => {
    // The studio switches runs by rewriting ?wr= and leaving the path alone, so
    // its URLs can carry a stale run id in the path. Flag-off must land on the
    // ?wr= run — straight onto the sub-path so the index redirect can't drop
    // ?active= (the shared selection).
    mocks.studioFlagState.mockReturnValue(false);
    renderAt(
      "/runs/wr_1?wr=wr_2&panes=editor,overview&active=act_9&selected-block=Payment",
    );
    expect(screen.getByTestId("location").textContent).toBe(
      "/runs/wr_2/overview?panes=editor,overview&active=act_9&selected-block=Payment",
    );
    expect(screen.getByTestId("legacy")).toBeTruthy();
    expect(screen.queryByTestId("studio")).toBeNull();
  });

  test("studio off: ?wr= naming the path run is not a redirect", () => {
    mocks.studioFlagState.mockReturnValue(false);
    renderAt("/runs/wr_1?wr=wr_1&active=act_9");
    expect(screen.getByTestId("location").textContent).toBe(
      "/runs/wr_1?wr=wr_1&active=act_9",
    );
    expect(screen.getByTestId("legacy")).toBeTruthy();
  });

  test("flag unresolved: no rewrite — a studio user's URL state must survive cold load", () => {
    mocks.studioFlagState.mockReturnValue(undefined);
    renderAt("/runs/wr_1?wr=wr_2&panes=editor,overview&active=act_9");
    expect(screen.getByTestId("location").textContent).toBe(
      "/runs/wr_1?wr=wr_2&panes=editor,overview&active=act_9",
    );
    expect(screen.getByTestId("legacy")).toBeTruthy();
  });

  test("studio off: /blocks maps to /overview and the studio-internal ?wrs=/?bl= are scrubbed", () => {
    // /blocks immediately re-navigates to /overview without the search, and
    // ?wrs=/?bl= are companions of the ?wr= being promoted into the path.
    mocks.studioFlagState.mockReturnValue(false);
    renderAt("/runs/wr_1/blocks?wr=wr_2&wrs=copilot&bl=Login&active=act_9");
    expect(screen.getByTestId("location").textContent).toBe(
      "/runs/wr_2/overview?active=act_9",
    );
  });

  test("studio off: the splat is whitelisted — known sub-paths forward, anything else lands on overview", () => {
    mocks.studioFlagState.mockReturnValue(false);
    const first = renderAt("/runs/wr_1/recording?wr=wr_2");
    expect(screen.getByTestId("location").textContent).toBe(
      "/runs/wr_2/recording",
    );
    first.unmount();
    renderAt("/runs/wr_1/%2E%2E%2Fagents?wr=wr_2&active=act_9");
    expect(screen.getByTestId("location").textContent).toBe(
      "/runs/wr_2/overview?active=act_9",
    );
  });

  test("studio off: a malformed ?wr= is ignored, not spliced into the path", () => {
    mocks.studioFlagState.mockReturnValue(false);
    const wr = encodeURIComponent("wr_2/../../agents/wpid_x/studio");
    renderAt(`/runs/wr_1?wr=${wr}`);
    expect(screen.getByTestId("location").textContent).toBe(
      `/runs/wr_1?wr=${wr}`,
    );
    expect(screen.getByTestId("legacy")).toBeTruthy();
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
