// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import CloudContext from "@/store/CloudContext";
import { PageSlotsProvider } from "@/store/PageSlots";
import { Status, type WorkflowRunApiResponse } from "@/api/types";
import { WorkflowPage } from "./WorkflowPage";

// cmdk (used by TagFilterControl) needs ResizeObserver + scrollIntoView, which
// jsdom lacks.
class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(globalThis as { ResizeObserver: unknown }).ResizeObserver = MockResizeObserver;
Element.prototype.scrollIntoView = () => {};

const workflowRun: WorkflowRunApiResponse = {
  created_at: "2026-07-08T00:00:00Z",
  failure_reason: null,
  started_at: "2026-07-08T00:00:00Z",
  finished_at: "2026-07-08T00:01:00Z",
  modified_at: "2026-07-08T00:01:00Z",
  proxy_location: null,
  script_run: false,
  status: Status.Completed,
  title: "My Run",
  trigger_type: null,
  webhook_callback_url: "",
  workflow_id: "wf_1",
  workflow_permanent_id: "wpid_abc123",
  workflow_run_id: "wr_1",
  workflow_title: "Test Workflow",
};

vi.mock("posthog-js/react", () => ({
  useFeatureFlagEnabled: () => false,
  useFeatureFlagVariantKey: () => undefined,
}));

const flagState = vi.hoisted(() => ({ taggingEnabled: true as boolean }));

vi.mock("@/hooks/useFeatureFlag", () => ({
  useFeatureFlag: () => flagState.taggingEnabled,
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

const workflowRunsQueryCalls: Array<Record<string, unknown>> = [];

vi.mock("./hooks/useWorkflowRunsQuery", () => ({
  useWorkflowRunsQuery: (props: Record<string, unknown>) => {
    workflowRunsQueryCalls.push(props);
    return {
      data: [workflowRun],
      isLoading: false,
    };
  },
}));

vi.mock("./hooks/useWorkflowTagsBatchQuery", () => ({
  useWorkflowTagsBatchQuery: () => ({ data: {} }),
}));

vi.mock("./hooks/useTagKeysQuery", () => ({
  useTagKeysQuery: () => ({ data: [] }),
}));

vi.mock("./hooks/useTagValuesQuery", () => ({
  useTagValuesQuery: () => ({ data: new Map() }),
}));

vi.mock("@/routes/tasks/hooks/useRunTagsBatchQuery", () => ({
  useRunTagsBatchQuery: () => ({
    data: { wr_1: [{ key: "skyvern.status", value: "completed" }] },
    isPending: false,
  }),
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

vi.mock("./components/tagging/TagChipList", () => ({
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
  workflowRunsQueryCalls.length = 0;
  flagState.taggingEnabled = true;
  cleanup();
  vi.clearAllMocks();
});

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="qs">{location.search}</output>;
}

function renderWorkflowPage(initialEntry = "/workflows/wpid_abc123") {
  return render(
    <CloudContext.Provider value={true}>
      <PageSlotsProvider value={{}}>
        <MemoryRouter initialEntries={[initialEntry]}>
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

describe("WorkflowPage run tags", () => {
  it("renders a run-tag chip on the agent runs list row", () => {
    const { container } = renderWorkflowPage();

    expect(
      within(container).getByTestId("tag-chip-list").textContent,
    ).toContain("completed");
  });
});

describe("WorkflowPage tag filter control", () => {
  it("drops a stale ?tags= param when tagging is disabled", () => {
    flagState.taggingEnabled = false;

    renderWorkflowPage("/workflows/wpid_abc123?tags=env:prod");

    const lastCall = workflowRunsQueryCalls[workflowRunsQueryCalls.length - 1];
    expect(lastCall?.tags).toBeUndefined();
  });

  it("filters by any value in a run-tag group", () => {
    const { container } = renderWorkflowPage();

    // TagFilterControl's popover content renders through a Radix portal
    // (appended to document.body), so it can't be queried via `within(container)`.
    fireEvent.click(screen.getByRole("button", { name: /tags/i }));
    fireEvent.click(screen.getByText("skyvern.platform"));

    const urlParams = new URLSearchParams(
      container.querySelector('[data-testid="qs"]')?.textContent ?? "",
    );
    expect(urlParams.get("tags")).toBe("skyvern.platform:*");
    expect(urlParams.get("page")).toBe("1");

    const lastCall = workflowRunsQueryCalls[workflowRunsQueryCalls.length - 1];
    expect(lastCall?.tags).toBe("skyvern.platform:*");
  });
});
