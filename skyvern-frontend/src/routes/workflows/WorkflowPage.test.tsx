// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  MemoryRouter,
  Route,
  Routes,
  RouterProvider,
  createMemoryRouter,
  useLocation,
} from "react-router-dom";

import { AxiosError, AxiosHeaders } from "axios";

import { Status, type WorkflowRunApiResponse } from "@/api/types";
import CloudContext from "@/store/CloudContext";
import { PageSlotsProvider, type PageSlots } from "@/store/PageSlots";
import { WorkflowPage } from "./WorkflowPage";

const { mockFeatureFlagEnabled, mockWorkflowRunsQuery, mockWorkflowQuery } =
  vi.hoisted(() => ({
    mockFeatureFlagEnabled: vi.fn(),
    mockWorkflowRunsQuery: vi.fn(),
    mockWorkflowQuery: vi.fn(),
  }));

vi.mock("posthog-js/react", () => ({
  useFeatureFlagEnabled: (flag: string) => mockFeatureFlagEnabled(flag),
  useFeatureFlagVariantKey: () => undefined,
}));
vi.mock("@/hooks/usePostHogFeatureFlag", () => ({
  usePostHogFeatureFlagEnabled: (flag: string) => mockFeatureFlagEnabled(flag),
  usePostHogFeatureFlagVariantKey: () => undefined,
}));

vi.mock("use-debounce", () => ({
  useDebounce: <T,>(value: T): [T] => [value],
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn(),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => vi.fn(),
}));

vi.mock("@/util/env", () => ({
  useNewRunsUrl: false,
}));

vi.mock("@/components/StatusFilterDropdown", () => ({
  StatusFilterDropdown: ({
    values,
    onChange,
  }: {
    values: Array<Status>;
    onChange: (values: Array<Status>) => void;
  }) => (
    <div data-testid="status-filter" data-values={values.join(",")}>
      <button type="button" onClick={() => onChange([Status.Failed])}>
        Filter failed
      </button>
      <button type="button" onClick={() => onChange([])}>
        Clear status filter
      </button>
    </div>
  ),
}));

vi.mock("@/components/TableSearchInput", () => ({
  TableSearchInput: ({
    value,
    onChange,
  }: {
    value: string;
    onChange: (value: string) => void;
  }) => (
    <input
      aria-label="Search runs by input"
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
}));

vi.mock("./WorkflowActions", () => ({
  WorkflowActions: () => <div data-testid="workflow-actions" />,
}));

vi.mock("./workflowRun/RunParametersDialog", () => ({
  RunParametersDialog: () => null,
}));

vi.mock("./workflowRun/WorkflowReliabilityPanel", () => ({
  WorkflowReliabilityPanel: () => null,
}));

vi.mock("./hooks/useWorkflowQuery", () => ({
  useWorkflowQuery: () =>
    mockWorkflowQuery() ?? {
      data: {
        title: "Test Workflow",
        workflow_definition: { parameters: [] },
      },
      isLoading: false,
    },
}));

vi.mock("./hooks/useGlobalWorkflowsQuery", () => ({
  useGlobalWorkflowsQuery: () => ({ data: [], isError: false }),
}));

vi.mock("./hooks/useWorkflowRunsQuery", () => ({
  useWorkflowRunsQuery: (props: unknown) => mockWorkflowRunsQuery(props),
}));

vi.mock("./hooks/useWorkflowTagsBatchQuery", () => ({
  useWorkflowTagsBatchQuery: () => ({ data: {} }),
}));

vi.mock("@/routes/tasks/hooks/useRunTagsBatchQuery", () => ({
  useRunTagsBatchQuery: () => ({ data: {} }),
}));

vi.mock("./hooks/useRunsHealSummaryBatchQuery", () => ({
  useRunsHealSummaryBatchQuery: () => ({ data: {} }),
}));

vi.mock("@/routes/tasks/hooks/useRunTagSuggestionsQuery", () => ({
  useRunTagSuggestionsQuery: () => ({
    data: { keys: [], valuesByKey: new Map(), labels: [] },
  }),
}));

vi.mock("./hooks/useTagKeysQuery", () => ({
  useTagKeysQuery: () => ({ data: [] }),
}));

vi.mock("./hooks/useTagValuesQuery", () => ({
  useTagValuesQuery: () => ({ data: new Map() }),
}));

vi.mock("./hooks/useKeywordSearch", () => ({
  useKeywordSearch: () => ({
    matchesParameter: () => false,
  }),
}));

vi.mock("./hooks/useParameterExpansion", () => ({
  useParameterExpansion: () => ({
    expandedRows: new Set<string>(),
    toggleExpanded: vi.fn(),
  }),
}));

vi.mock("@/store/onboarding/useOnboardingState", () => ({
  useOnboardingState: () => ({
    state: null,
    isLoading: false,
    updateState: vi.fn(),
    isNewUser: false,
    abVariant: null,
  }),
  useOnboardingStateOptional: () => ({
    state: null,
    isLoading: false,
    updateState: vi.fn(),
    isNewUser: false,
    abVariant: null,
  }),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  mockWorkflowQuery.mockReset();
});

type RenderOptions = {
  isCloud?: boolean;
  pageSlots?: PageSlots;
  initialEntries?: Array<string>;
  workflowRuns?: Array<WorkflowRunApiResponse>;
  // Returned to the unfiltered single-run lookup a 404ing agent makes.
  latestRuns?: Array<WorkflowRunApiResponse>;
  latestRunsError?: boolean;
};

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-search" data-search={location.search} />;
}

function renderWorkflowPage({
  isCloud = true,
  pageSlots = {},
  initialEntries = ["/workflows/wpid_abc123"],
  workflowRuns = [],
  latestRuns = workflowRuns,
  latestRunsError = false,
}: RenderOptions = {}) {
  mockFeatureFlagEnabled.mockReturnValue(true);
  mockWorkflowRunsQuery.mockImplementation(
    ({ pageSize }: { pageSize?: number }) =>
      pageSize === 1 && latestRunsError
        ? { data: undefined, isLoading: false, isError: true }
        : {
            data: pageSize === 1 ? latestRuns : workflowRuns,
            isLoading: false,
          },
  );

  return render(
    <CloudContext.Provider value={isCloud}>
      <PageSlotsProvider value={pageSlots}>
        <MemoryRouter initialEntries={initialEntries}>
          <Routes>
            <Route
              path="/workflows/:workflowPermanentId"
              element={
                <>
                  <WorkflowPage />
                  <LocationProbe />
                </>
              }
            />
          </Routes>
        </MemoryRouter>
      </PageSlotsProvider>
    </CloudContext.Provider>,
  );
}

function makeWorkflowRun(
  overrides: Partial<WorkflowRunApiResponse>,
): WorkflowRunApiResponse {
  return {
    created_at: "2026-06-01T12:00:00.000Z",
    failure_reason: null,
    started_at: "2026-06-01T12:00:01.000Z",
    finished_at: "2026-06-01T12:00:10.000Z",
    modified_at: "2026-06-01T12:00:10.000Z",
    proxy_location: null,
    script_run: false,
    status: Status.Completed,
    webhook_callback_url: "",
    workflow_id: "wf_abc123",
    workflow_permanent_id: "wpid_abc123",
    workflow_run_id: "wr_default",
    workflow_title: "Test Workflow",
    retried_from_workflow_run_id: null,
    ...overrides,
  };
}

describe("WorkflowPage analytics button", () => {
  it("shows the Analytics link for cloud users", () => {
    renderWorkflowPage();

    const analyticsLink = screen.getByRole("link", { name: /analytics/i });
    expect(analyticsLink.getAttribute("href")).toBe(
      "/analytics?workflow=wpid_abc123",
    );
  });

  it("hides the Analytics link outside the cloud app", () => {
    renderWorkflowPage({ isCloud: false });

    expect(screen.queryByRole("link", { name: /analytics/i })).toBeNull();
  });

  it("renders the injected workflow analytics panel above Past Runs", () => {
    const PanelStub = () => <div data-testid="analytics-panel-stub" />;
    const { container } = renderWorkflowPage({
      pageSlots: { workflowAnalyticsPanel: PanelStub },
    });

    expect(
      container.querySelector('[data-testid="analytics-panel-stub"]'),
    ).not.toBeNull();
  });

  it("renders the injected workflow runs filter controls in the Past Runs row", () => {
    const FilterStub = () => <div data-testid="filter-controls-stub" />;
    const { container } = renderWorkflowPage({
      pageSlots: { workflowRunsFilterControls: FilterStub },
    });

    expect(
      container.querySelector('[data-testid="filter-controls-stub"]'),
    ).not.toBeNull();
  });

  it("preserves period/from/to while an empty page rolls back", () => {
    // The mocked useWorkflowRunsQuery always returns an empty page, so the
    // rollback effect cascades all the way to page=1 — this still proves the
    // fix, since period/from/to must survive every intermediate replacement.
    const { container } = renderWorkflowPage({
      initialEntries: [
        "/workflows/wpid_abc123?period=custom&from=2026-06-01&to=2026-06-03&page=3",
      ],
    });

    const search = container
      .querySelector('[data-testid="location-search"]')
      ?.getAttribute("data-search");
    expect(search).toContain("page=1");
    expect(search).toContain("period=custom");
    expect(search).toContain("from=2026-06-01");
    expect(search).toContain("to=2026-06-03");
  });

  it("shows the fallback retry badge only for runs retried from another run", () => {
    renderWorkflowPage({
      workflowRuns: [
        makeWorkflowRun({
          workflow_run_id: "wr_retry",
          retried_from_workflow_run_id: "wr_original",
        }),
        makeWorkflowRun({ workflow_run_id: "wr_original" }),
      ],
    });

    const retryRow = screen.getByText("wr_retry").closest("tr");
    const originalRow = screen.getByText("wr_original").closest("tr");

    expect(retryRow).not.toBeNull();
    expect(originalRow).not.toBeNull();
    expect(
      within(retryRow!).getByLabelText(
        "Automatic retry with fallback credential",
      ),
    ).not.toBeNull();
    expect(
      within(originalRow!).queryByLabelText(
        "Automatic retry with fallback credential",
      ),
    ).toBeNull();
  });
});

describe("Past Runs list state in the URL", () => {
  function locationSearch(container: HTMLElement): string {
    return (
      container
        .querySelector('[data-testid="location-search"]')
        ?.getAttribute("data-search") ?? ""
    );
  }

  it("restores the search term and status filter from the URL", () => {
    const { container } = renderWorkflowPage({
      initialEntries: ["/workflows/wpid_abc123?search=invoice&status=failed"],
      workflowRuns: [makeWorkflowRun({ workflow_run_id: "wr_match" })],
    });

    expect(
      (screen.getByLabelText("Search runs by input") as HTMLInputElement).value,
    ).toBe("invoice");
    expect(
      container
        .querySelector('[data-testid="status-filter"]')
        ?.getAttribute("data-values"),
    ).toBe("failed");
    expect(mockWorkflowRunsQuery).toHaveBeenCalledWith(
      expect.objectContaining({
        search: "invoice",
        statusFilters: [Status.Failed],
      }),
    );
  });

  it("writes the search term to the URL and drops it once cleared", () => {
    const { container } = renderWorkflowPage({
      initialEntries: ["/workflows/wpid_abc123?page=2"],
      // A non-empty page keeps the empty-page rollback effect from rewriting
      // ?page= on its own, so the reset below is the search handler's doing.
      workflowRuns: [makeWorkflowRun({ workflow_run_id: "wr_1" })],
    });
    const input = screen.getByLabelText("Search runs by input");

    fireEvent.change(input, { target: { value: "invoice" } });

    expect(locationSearch(container)).toContain("search=invoice");
    expect(locationSearch(container)).toContain("page=1");
    expect(
      (screen.getByLabelText("Search runs by input") as HTMLInputElement).value,
    ).toBe("invoice");

    fireEvent.change(input, { target: { value: "" } });

    expect(locationSearch(container)).not.toContain("search=");
  });

  it("restores the query after opening a run and going back", async () => {
    mockFeatureFlagEnabled.mockReturnValue(true);
    mockWorkflowRunsQuery.mockReturnValue({
      data: [makeWorkflowRun({ workflow_run_id: "wr_1" })],
      isLoading: false,
    });
    const router = createMemoryRouter(
      [
        { path: "/workflows/:workflowPermanentId", element: <WorkflowPage /> },
        { path: "*", element: <div>run detail</div> },
      ],
      { initialEntries: ["/workflows/wpid_abc123"] },
    );

    render(
      <CloudContext.Provider value={true}>
        <PageSlotsProvider value={{}}>
          <RouterProvider router={router} />
        </PageSlotsProvider>
      </CloudContext.Provider>,
    );

    fireEvent.change(screen.getByLabelText("Search runs by input"), {
      target: { value: "invoice" },
    });
    fireEvent.click(screen.getByText("wr_1"));
    await waitFor(() => expect(screen.getByText("run detail")).toBeTruthy());

    await act(async () => {
      await router.navigate(-1);
    });

    expect(
      (screen.getByLabelText("Search runs by input") as HTMLInputElement).value,
    ).toBe("invoice");
    expect(mockWorkflowRunsQuery).toHaveBeenLastCalledWith(
      expect.objectContaining({ search: "invoice" }),
    );
  });

  it("writes the status filter to the URL and drops it once cleared", () => {
    const { container } = renderWorkflowPage();

    fireEvent.click(screen.getByRole("button", { name: "Filter failed" }));

    expect(locationSearch(container)).toContain("status=failed");

    fireEvent.click(
      screen.getByRole("button", { name: "Clear status filter" }),
    );

    expect(locationSearch(container)).not.toContain("status=");
  });
});

describe("agent that 404s", () => {
  // Soft-deleted agents 404 on GET /workflows/{id} but keep their run history.
  function mockWorkflowNotFound() {
    mockWorkflowQuery.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new AxiosError("Not Found", "404", undefined, undefined, {
        status: 404,
        statusText: "Not Found",
        data: {},
        headers: {},
        config: { headers: new AxiosHeaders() },
      }),
    });
  }

  it("keeps the deleted agent's history when the filtered list is empty", () => {
    mockWorkflowNotFound();
    renderWorkflowPage({
      initialEntries: ["/workflows/wpid_abc123?period=7d&status=failed"],
      workflowRuns: [],
      latestRuns: [makeWorkflowRun({ workflow_title: "Old Agent" })],
    });

    expect(screen.getByText("Old Agent (deleted)")).toBeTruthy();
    expect(screen.getByText("Past Runs")).toBeTruthy();
    expect(screen.queryByText("Not Found")).toBeNull();
    for (const name of ["Edit", "Run"]) {
      expect(screen.queryByRole("link", { name })).toBeNull();
    }
  });

  it("keeps the page when the latest-run lookup fails", () => {
    mockWorkflowNotFound();
    renderWorkflowPage({
      workflowRuns: [
        makeWorkflowRun({
          workflow_run_id: "wr_kept",
          workflow_title: "Old Agent",
        }),
      ],
      latestRunsError: true,
    });

    expect(screen.getByText("wr_kept")).toBeTruthy();
    expect(screen.getByText("Old Agent (deleted)")).toBeTruthy();
    expect(screen.queryByText("Not Found")).toBeNull();
  });

  it("shows the 404 page when the agent has no runs", () => {
    mockWorkflowNotFound();
    // The cloud app always adds a default ?period= window.
    renderWorkflowPage({
      initialEntries: ["/workflows/wpid_abc123?period=365d&page=1"],
    });

    expect(screen.getByText("Not Found")).toBeTruthy();
    expect(screen.queryByText("Past Runs")).toBeNull();
  });
});

it("refetches the history on mount without adding list polling", () => {
  renderWorkflowPage();
  const options = mockWorkflowRunsQuery.mock.lastCall?.[0];
  expect(options.refetchOnMount).toBe("always");
  expect(options.refetchInterval).toBeUndefined();
});
