// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { WorkflowPage } from "./WorkflowPage";

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
  TableSearchInput: () => <input aria-label="Search runs by parameter" />,
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

function renderWorkflowPage() {
  return render(
    <MemoryRouter initialEntries={["/workflows/wpid_abc123"]}>
      <Routes>
        <Route
          path="/workflows/:workflowPermanentId"
          element={<WorkflowPage />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe("WorkflowPage analytics button", () => {
  it("does not render the Analytics link", () => {
    renderWorkflowPage();

    expect(screen.queryByRole("link", { name: /analytics/i })).toBeNull();
  });
});
