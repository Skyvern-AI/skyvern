// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { Status } from "@/api/types";

const { workflowQueryMock, runQueryMock, runsQueryMock } = vi.hoisted(() => ({
  workflowQueryMock: vi.fn(),
  runQueryMock: vi.fn(),
  runsQueryMock: vi.fn(),
}));

vi.mock("@/hooks/useWorkflowStudioEnabled", () => ({
  useWorkflowStudioEnabled: () => true,
}));
vi.mock("../hooks/useWorkflowQuery", () => ({
  useWorkflowQuery: () => workflowQueryMock(),
}));
vi.mock("../hooks/useGlobalWorkflowsQuery", () => ({
  useGlobalWorkflowsQuery: () => ({ data: [], isLoading: false }),
}));
vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => runQueryMock(),
}));
vi.mock("../hooks/useWorkflowRunsQuery", () => ({
  useWorkflowRunsQuery: () => runsQueryMock(),
}));
// Pane bodies own their data wiring (tested in their own suites); the shell
// contract under test is which of them mount and how the chrome degrades.
vi.mock("../studio/EditorTab", () => ({
  EditorTab: () => <div data-testid="editor-tab-body" />,
}));
vi.mock("../studio/BrowserTab", () => ({
  BrowserTab: () => <div data-testid="browser-tab-body" />,
}));
vi.mock("../studio/RunTab", () => ({
  RunTab: () => <div data-testid="run-view-body" />,
}));
vi.mock("../studio/StudioBrowserStream", () => ({
  StudioBrowserStream: () => null,
}));
vi.mock("../studio/StudioWorkflowPanels", () => ({
  StudioWorkflowPanels: () => null,
}));
vi.mock("../studio/runview/RunPaneHeader", () => ({
  RunPaneViewToggles: () => null,
  RunPaneActions: () => null,
}));
vi.mock("@/components/onboarding/ProductTour", () => ({
  ProductTour: () => null,
}));

import { WorkflowEditor } from "./WorkflowEditor";

const deletedWorkflow = {
  workflow_permanent_id: "wpid_del",
  title: "Deleted agent",
  deleted_at: "2026-07-01T12:00:00Z",
  workflow_definition: { blocks: [], parameters: [] },
  proxy_location: null,
  webhook_callback_url: null,
  persist_browser_session: false,
  cache_key: "",
} as never;

const liveWorkflow = {
  workflow_permanent_id: "wpid_live",
  title: "Live agent",
  deleted_at: null,
  workflow_definition: { blocks: [], parameters: [] },
  proxy_location: null,
  webhook_callback_url: null,
  persist_browser_session: false,
  cache_key: "",
} as never;

function makeRun(workflow: unknown) {
  return {
    workflow_run_id: "wr_1",
    status: Status.Completed,
    parameters: {},
    workflow,
  } as never;
}

function renderStudioAt(path: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route
            path="/agents/:workflowPermanentId/studio"
            element={<WorkflowEditor />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  runsQueryMock.mockReturnValue({ data: [], isPending: false });
});

describe("WorkflowEditor deleted-agent run fallback", () => {
  test("renders the studio run view from the run snapshot when the workflow query 404s", () => {
    workflowQueryMock.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
    });
    runQueryMock.mockReturnValue({
      data: makeRun(deletedWorkflow),
      isLoading: false,
    });

    renderStudioAt("/agents/wpid_del/studio?wr=wr_1");

    // The run-viewing surfaces are up (this used to render nothing at all).
    expect(screen.getByTestId("run-view-body")).toBeTruthy();
    expect(screen.getByTestId("browser-tab-body")).toBeTruthy();

    // Workflow-mutating surfaces degrade: legacy tag, no editor/copilot
    // bodies, blocked toggles, no save/run actions.
    expect(screen.getByText(/Agent deleted on/)).toBeTruthy();
    expect(screen.queryByTestId("editor-tab-body")).toBeNull();
    expect(
      screen.getAllByText("Source agent deleted — this run is view-only."),
    ).toHaveLength(2);
    expect(
      (screen.getByRole("button", { name: "Copilot" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Editor" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(screen.queryByLabelText("Save workflow")).toBeNull();
    expect(screen.queryByRole("button", { name: /^Run agent$/ })).toBeNull();
  });

  test("stays on the loading pulse while the run fallback is still loading", () => {
    workflowQueryMock.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
    });
    runQueryMock.mockReturnValue({ data: undefined, isLoading: true });

    renderStudioAt("/agents/wpid_del/studio?wr=wr_1");

    expect(screen.queryByTestId("run-view-body")).toBeNull();
    expect(screen.queryByText(/Agent deleted on/)).toBeNull();
  });

  test("a healthy workflow keeps the full studio (no deleted degradation)", () => {
    workflowQueryMock.mockReturnValue({
      data: liveWorkflow,
      isLoading: false,
      isError: false,
    });
    runQueryMock.mockReturnValue({
      data: makeRun(liveWorkflow),
      isLoading: false,
    });

    renderStudioAt("/agents/wpid_live/studio?wr=wr_1");

    expect(screen.getByTestId("editor-tab-body")).toBeTruthy();
    expect(screen.queryByText(/Agent deleted on/)).toBeNull();
    expect(
      screen.queryByText("Source agent deleted — this run is view-only."),
    ).toBeNull();
    expect(screen.getByLabelText("Save workflow")).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "Copilot" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });
});
