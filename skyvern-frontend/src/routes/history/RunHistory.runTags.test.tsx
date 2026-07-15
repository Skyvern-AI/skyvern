// @vitest-environment jsdom

import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TaskRunType, type TaskRunListItem } from "@/api/types";

// cmdk (used by TagFilterControl) needs ResizeObserver + scrollIntoView, which
// jsdom lacks.
class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(globalThis as { ResizeObserver: unknown }).ResizeObserver = MockResizeObserver;
Element.prototype.scrollIntoView = () => {};

const run: TaskRunListItem = {
  task_run_id: "tr_1",
  task_run_type: TaskRunType.WorkflowRun,
  run_id: "wr_1",
  title: "My Run",
  status: "completed",
  started_at: "2026-07-08T00:00:00Z",
  finished_at: "2026-07-08T00:01:00Z",
  created_at: "2026-07-08T00:00:00Z",
  workflow_permanent_id: "wpid_1",
  workflow_deleted: false,
  script_run: false,
  trigger_type: null,
  searchable_text: null,
};

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
    runsQueryCalls.push(props);
    return { data: [run], isFetching: false };
  },
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => vi.fn(),
}));

const flagState = vi.hoisted(() => ({ taggingEnabled: true as boolean }));

vi.mock("@/hooks/useFeatureFlag", () => ({
  useFeatureFlag: () => flagState.taggingEnabled,
}));

vi.mock("@/routes/workflows/hooks/useGlobalWorkflowsQuery", () => ({
  useGlobalWorkflowsQuery: () => ({ data: [] }),
}));

vi.mock("@/store/onboarding/useOnboardingState", () => ({
  useOnboardingStateOptional: () => null,
}));

vi.mock("@/components/StatusFilterDropdown", () => ({
  StatusFilterDropdown: () => <div data-testid="status-filter" />,
}));

vi.mock("@/components/onboarding/OnboardingEmptyState", () => ({
  OnboardingEmptyState: () => <div data-testid="onboarding-empty" />,
}));

vi.mock("@/components/TableSearchInput", () => ({
  TableSearchInput: () => <input aria-label="search-runs" readOnly />,
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn(async () => ({
    get: vi.fn(async () => ({
      data: { parameters: {}, extra_http_headers: null },
    })),
  })),
}));

vi.mock("@/routes/tasks/hooks/useRunTagsBatchQuery", () => ({
  useRunTagsBatchQuery: () => ({
    data: { wr_1: [{ key: "skyvern.platform", value: "platform_a" }] },
    isPending: false,
  }),
}));

vi.mock("@/routes/workflows/hooks/useTagKeysQuery", () => ({
  useTagKeysQuery: () => ({ data: [] }),
}));

vi.mock("@/routes/workflows/hooks/useTagValuesQuery", () => ({
  useTagValuesQuery: () => ({ data: new Map() }),
}));

vi.mock("@/routes/tasks/hooks/useRunTagSuggestionsQuery", () => ({
  useRunTagSuggestionsQuery: () => ({
    data: {
      keys: ["skyvern.platform"],
      valuesByKey: new Map([["skyvern.platform", ["platform_a"]]]),
      labels: ["adhoc"],
    },
  }),
}));

vi.mock("@/routes/workflows/components/tagging/TagChipList", () => ({
  TagChipList: ({
    tags,
  }: {
    tags: Array<{ key: string | null; value: string }>;
  }) => (
    <span data-testid="tag-chip-list">
      {tags.map((tag) => `${tag.key ?? "label"}:${tag.value}`).join(",")}
    </span>
  ),
}));

import { RunHistory } from "./RunHistory";

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/history"]}>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="qs">{location.search}</output>;
}

afterEach(() => {
  runsQueryCalls.length = 0;
  flagState.taggingEnabled = true;
  vi.clearAllMocks();
  cleanup();
});

describe("RunHistory run tags", () => {
  it("renders a run-tag chip on the all-runs list", () => {
    const { container } = render(<RunHistory />, { wrapper });

    expect(
      within(container).getByTestId("tag-chip-list").textContent,
    ).toContain("platform_a");
  });
});

describe("RunHistory tag filter control", () => {
  it("drops a stale ?tags= param when tagging is disabled", () => {
    flagState.taggingEnabled = false;

    function disabledWrapper({ children }: { children: ReactNode }) {
      const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      return (
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={["/history?tags=adhoc"]}>
            {children}
          </MemoryRouter>
        </QueryClientProvider>
      );
    }

    render(<RunHistory />, { wrapper: disabledWrapper });

    const lastCall = runsQueryCalls[runsQueryCalls.length - 1];
    expect(lastCall?.tags).toBeUndefined();
  });

  it("filters by a standalone label", () => {
    const { container } = render(
      <>
        <RunHistory />
        <LocationProbe />
      </>,
      { wrapper },
    );

    // TagFilterControl's popover content renders through a Radix portal
    // (appended to document.body), so it can't be queried via `within(container)`.
    fireEvent.click(screen.getByRole("button", { name: /tags/i }));
    fireEvent.change(screen.getByPlaceholderText(/filter by/i), {
      target: { value: "adhoc" },
    });
    fireEvent.click(screen.getByText(/^adhoc$/));

    const urlParams = new URLSearchParams(
      container.querySelector('[data-testid="qs"]')?.textContent ?? "",
    );
    expect(urlParams.get("tags")).toBe("adhoc");
    expect(urlParams.get("page")).toBe("1");

    const lastCall = runsQueryCalls[runsQueryCalls.length - 1];
    expect(lastCall?.tags).toBe("adhoc");
  });
});
