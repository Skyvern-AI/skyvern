// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import CloudContext from "@/store/CloudContext";
import { WorkflowPage } from "./WorkflowPage";

const { mockFeatureFlagEnabled } = vi.hoisted(() => ({
  mockFeatureFlagEnabled: vi.fn(),
}));

vi.mock("posthog-js/react", () => ({
  useFeatureFlagEnabled: (flag: string) => mockFeatureFlagEnabled(flag),
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

vi.mock("./hooks/useTagKeysQuery", () => ({
  useTagKeysQuery: () => ({ data: [] }),
}));

vi.mock("./hooks/useKeywordSearch", () => ({
  useKeywordSearch: () => ({
    matchesParameter: () => false,
    isSearchActive: false,
  }),
}));

vi.mock("./hooks/useParameterExpansion", () => ({
  useParameterExpansion: () => ({
    expandedRows: new Set<string>(),
    toggleExpanded: vi.fn(),
    setAutoExpandedRows: vi.fn(),
  }),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

type RenderOptions = {
  isCloud?: boolean;
  analyticsFlagEnabled?: boolean;
};

function renderWorkflowPage({
  isCloud = true,
  analyticsFlagEnabled = true,
}: RenderOptions = {}) {
  mockFeatureFlagEnabled.mockReturnValue(analyticsFlagEnabled);

  return render(
    <CloudContext.Provider value={isCloud}>
      <MemoryRouter initialEntries={["/workflows/wpid_abc123"]}>
        <Routes>
          <Route
            path="/workflows/:workflowPermanentId"
            element={<WorkflowPage />}
          />
        </Routes>
      </MemoryRouter>
    </CloudContext.Provider>,
  );
}

describe("WorkflowPage analytics button", () => {
  it("shows the Analytics link for cloud users when the dashboard flag is enabled", () => {
    renderWorkflowPage();

    const analyticsLink = screen.getByRole("link", { name: /analytics/i });
    expect(analyticsLink.getAttribute("href")).toBe(
      "/analytics?compare=wpid_abc123",
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
});
