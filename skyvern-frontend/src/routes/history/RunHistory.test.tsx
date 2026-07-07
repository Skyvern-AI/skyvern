// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { TaskRunType } from "@/api/types";
import { RunHistory } from "./RunHistory";

const workflowRun = {
  task_run_id: "tr_1",
  task_run_type: TaskRunType.WorkflowRun,
  run_id: "wr_123",
  title: "My Run",
  status: "completed",
  started_at: "2026-06-14T10:00:00Z",
  finished_at: "2026-06-14T10:01:00Z",
  created_at: "2026-06-14T10:00:00Z",
  workflow_permanent_id: "wpid_1",
  workflow_deleted: false,
  script_run: false,
  searchable_text: "city Paris",
};

// Stable references so an active search doesn't churn the runs identity
// across renders (which would retrigger effects under test).
const runsData = [workflowRun];
const runsQueryResult = { data: runsData, isFetching: false };

vi.mock("use-debounce", () => ({
  useDebounce: <T,>(value: T): [T] => [value],
}));

vi.mock("posthog-js/react", () => ({
  useFeatureFlagVariantKey: () => undefined,
  useFeatureFlagEnabled: () => false,
}));

vi.mock("@/hooks/useRunsQuery", () => ({
  useRunsQuery: () => runsQueryResult,
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => vi.fn(),
}));

vi.mock("@/routes/workflows/hooks/useGlobalWorkflowsQuery", () => ({
  useGlobalWorkflowsQuery: () => ({ data: [] }),
}));

vi.mock("@/components/StatusFilterDropdown", () => ({
  StatusFilterDropdown: () => <div data-testid="status-filter" />,
}));

vi.mock("@/components/onboarding/OnboardingEmptyState", () => ({
  OnboardingEmptyState: () => <div data-testid="onboarding-empty" />,
}));

vi.mock("@/store/onboarding/useOnboardingState", () => ({
  useOnboardingStateOptional: () => null,
}));

vi.mock("@/components/TableSearchInput", () => ({
  TableSearchInput: ({
    value,
    onChange,
    placeholder,
    disabled,
  }: {
    value: string;
    onChange: (value: string) => void;
    placeholder?: string;
    disabled?: boolean;
  }) => (
    <input
      aria-label="search-runs"
      value={value}
      placeholder={placeholder}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn(async () => ({
    get: vi.fn(async () => ({
      data: { parameters: { city: "Paris" }, extra_http_headers: null },
    })),
  })),
}));

function renderRunHistory() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/history"]}>
        <RunHistory />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function activateSearch() {
  fireEvent.change(screen.getByLabelText("search-runs"), {
    target: { value: "city" },
  });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("RunHistory inputs during filtering", () => {
  it("keeps Run Inputs collapsed while a search is active", () => {
    renderRunHistory();
    activateSearch();

    // The run is in the results, but its inputs pane must not auto-expand:
    // only the header row and the single data row should be present.
    expect(screen.getByText("wr_123")).toBeTruthy();
    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(screen.queryByText("Run Inputs")).toBeNull();
  });

  it("still lets the user expand Run Inputs during a search", async () => {
    renderRunHistory();
    activateSearch();

    const runRow = screen.getByText("wr_123").closest("tr");
    expect(runRow).not.toBeNull();
    fireEvent.click(within(runRow as HTMLElement).getByRole("button"));

    expect(await screen.findByText("Run Inputs")).toBeTruthy();
    expect(screen.getByText("Paris")).toBeTruthy();
  });
});
