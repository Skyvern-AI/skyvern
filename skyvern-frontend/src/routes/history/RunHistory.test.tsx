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

import { TaskRunType, TriggerType, type TaskRunListItem } from "@/api/types";
import { RunHistory } from "./RunHistory";

const workflowRun: TaskRunListItem = {
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
  trigger_type: null,
  searchable_text: "city Paris",
};

// Stable references so an active search doesn't churn the runs identity
// across renders (which would retrigger effects under test).
const runsData = [workflowRun];
const runsQueryResult = { data: runsData, isFetching: false };

const { useRunsQuerySpy } = vi.hoisted(() => ({ useRunsQuerySpy: vi.fn() }));

vi.mock("use-debounce", () => ({
  useDebounce: <T,>(value: T): [T] => [value],
}));

vi.mock("posthog-js/react", () => ({
  useFeatureFlagVariantKey: () => undefined,
  useFeatureFlagEnabled: () => false,
}));

const runsQueryCalls: Array<Record<string, unknown>> = [];

vi.mock("@/hooks/useRunsQuery", () => ({
  useRunsQuery: (props: Record<string, unknown>) => {
    useRunsQuerySpy(props);
    runsQueryCalls.push(props);
    return runsQueryResult;
  },
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => vi.fn(),
}));

vi.mock("@/hooks/useFeatureFlag", () => ({
  useFeatureFlag: () => true,
}));

vi.mock("@/routes/tasks/hooks/useRunTagsBatchQuery", () => ({
  useRunTagsBatchQuery: () => ({ data: {} }),
}));

vi.mock("@/routes/workflows/hooks/useTagKeysQuery", () => ({
  useTagKeysQuery: () => ({ data: [] }),
}));

vi.mock("@/routes/workflows/hooks/useTagValuesQuery", () => ({
  useTagValuesQuery: () => ({ data: new Map() }),
}));

vi.mock("@/routes/workflows/hooks/useGlobalWorkflowsQuery", () => ({
  useGlobalWorkflowsQuery: () => ({ data: [] }),
}));

vi.mock("@/components/StatusFilterDropdown", () => ({
  StatusFilterDropdown: () => <div data-testid="status-filter" />,
}));

vi.mock("@/components/RunTypeFilterDropdown", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@/components/RunTypeFilterDropdown")>();
  return {
    ...actual,
    RunTypeFilterDropdown: ({
      values,
      onChange,
    }: {
      values: Array<string>;
      onChange: (values: Array<string>) => void;
    }) => (
      <button
        data-testid="run-type-filter"
        onClick={() =>
          onChange(
            values.includes("agent")
              ? values.filter((value) => value !== "agent")
              : [...values, "agent"],
          )
        }
      >
        toggle-agent
      </button>
    ),
  };
});

vi.mock("@/components/TriggerTypeBadge", () => ({
  TriggerTypeBadge: ({ triggerType }: { triggerType: string }) => (
    <span data-testid={`trigger-type-${triggerType}`}>{triggerType}</span>
  ),
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
  runsData.splice(0, runsData.length, workflowRun);
  runsQueryCalls.length = 0;
  cleanup();
  vi.clearAllMocks();
});

describe("RunHistory search highlighting", () => {
  it("does not highlight the run id for a sub-3-char query", () => {
    const { container } = renderRunHistory();
    fireEvent.change(screen.getByLabelText("search-runs"), {
      target: { value: "12" },
    });

    expect(container.innerHTML).not.toContain("bg-blue-500/30");
  });

  it("highlights the run id once the query reaches 3 chars", () => {
    const { container } = renderRunHistory();
    fireEvent.change(screen.getByLabelText("search-runs"), {
      target: { value: "123" },
    });

    expect(container.innerHTML).toContain("bg-blue-500/30");
  });

  it("sends a trimmed search value to the runs query, not just a trimmed length check", () => {
    renderRunHistory();
    fireEvent.change(screen.getByLabelText("search-runs"), {
      target: { value: " 123" },
    });

    const calls = useRunsQuerySpy.mock.calls;
    const lastCall = calls[calls.length - 1]?.[0] as { search?: string };
    expect(lastCall.search).toBe("123");
  });
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

  it("renders the MCP trigger badge from persisted trigger_type", () => {
    runsData.splice(0, runsData.length, {
      ...workflowRun,
      trigger_type: TriggerType.Mcp,
    });

    renderRunHistory();

    expect(screen.getByTestId("trigger-type-mcp")).toBeTruthy();
  });

  it("expands the selected run-type group into raw run types for the runs query", () => {
    renderRunHistory();

    expect(runsQueryCalls[runsQueryCalls.length - 1]?.runTypeFilters).toEqual(
      [],
    );

    fireEvent.click(screen.getByTestId("run-type-filter"));

    // The curated Agent group expands to the engine-specific CUA run types.
    expect(runsQueryCalls[runsQueryCalls.length - 1]?.runTypeFilters).toEqual([
      TaskRunType.OpenaiCua,
      TaskRunType.AnthropicCua,
      TaskRunType.UiTars,
      TaskRunType.YutoriNavigator,
    ]);

    fireEvent.click(screen.getByTestId("run-type-filter"));

    expect(runsQueryCalls[runsQueryCalls.length - 1]?.runTypeFilters).toEqual(
      [],
    );
  });
});
