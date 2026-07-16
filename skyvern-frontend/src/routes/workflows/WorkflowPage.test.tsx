// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import CloudContext from "@/store/CloudContext";
import { PageSlotsProvider, type PageSlots } from "@/store/PageSlots";
import { WorkflowPage } from "./WorkflowPage";

const { mockFeatureFlagEnabled } = vi.hoisted(() => ({
  mockFeatureFlagEnabled: vi.fn(),
}));

vi.mock("posthog-js/react", () => ({
  useFeatureFlagEnabled: (flag: string) => mockFeatureFlagEnabled(flag),
  useFeatureFlagVariantKey: () => undefined,
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
  StatusFilterDropdown: () => <div data-testid="status-filter" />,
}));

vi.mock("@/components/TableSearchInput", () => ({
  TableSearchInput: () => <input aria-label="Search runs by input" />,
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
  useWorkflowQuery: () => ({
    data: {
      title: "Test Workflow",
      workflow_definition: { parameters: [] },
    },
    isLoading: false,
  }),
}));

vi.mock("./hooks/useWorkflowRunsQuery", () => ({
  useWorkflowRunsQuery: () => ({
    data: [],
    isLoading: false,
  }),
}));

vi.mock("./hooks/useWorkflowTagsBatchQuery", () => ({
  useWorkflowTagsBatchQuery: () => ({ data: {} }),
}));

vi.mock("@/routes/tasks/hooks/useRunTagsBatchQuery", () => ({
  useRunTagsBatchQuery: () => ({ data: {} }),
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
});

type RenderOptions = {
  isCloud?: boolean;
  analyticsFlagEnabled?: boolean;
  pageSlots?: PageSlots;
  initialEntries?: Array<string>;
};

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-search" data-search={location.search} />;
}

function renderWorkflowPage({
  isCloud = true,
  analyticsFlagEnabled = true,
  pageSlots = {},
  initialEntries = ["/workflows/wpid_abc123"],
}: RenderOptions = {}) {
  mockFeatureFlagEnabled.mockReturnValue(analyticsFlagEnabled);

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

describe("WorkflowPage analytics button", () => {
  it("shows the Analytics link for cloud users when the dashboard flag is enabled", () => {
    renderWorkflowPage();

    const analyticsLink = screen.getByRole("link", { name: /analytics/i });
    expect(analyticsLink.getAttribute("href")).toBe(
      "/analytics?workflow=wpid_abc123",
    );
    expect(mockFeatureFlagEnabled).toHaveBeenCalledWith("ANALYTICS_DASHBOARD");
  });

  it("hides the Analytics link when the dashboard flag is disabled", () => {
    renderWorkflowPage({ analyticsFlagEnabled: false });

    expect(screen.queryByRole("link", { name: /analytics/i })).toBeNull();
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
});
