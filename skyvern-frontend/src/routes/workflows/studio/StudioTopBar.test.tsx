// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { ProxyLocation, Status } from "@/api/types";
import { TooltipProvider } from "@/components/ui/tooltip";

const { workflowRunQueryMock } = vi.hoisted(() => ({
  workflowRunQueryMock: vi.fn(),
}));

vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => workflowRunQueryMock(),
}));
vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => vi.fn(),
}));

import { RunStopButton } from "./StudioTopBar";

function LocationProbe() {
  const location = useLocation();
  return (
    <>
      <div data-testid="location">
        {location.pathname}
        {location.search}
      </div>
      <div data-testid="location-state">
        {JSON.stringify(location.state ?? null)}
      </div>
    </>
  );
}

function renderAt(path: string, element = <RunStopButton />) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={0}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route
              path="/workflows/:workflowPermanentId/studio"
              element={element}
            />
            <Route
              path="/agents/:workflowPermanentId/run"
              element={<LocationProbe />}
            />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

function mockRun(status: Status, overrides: Record<string, unknown> = {}) {
  workflowRunQueryMock.mockReturnValue({
    data: {
      workflow_run_id: "wr_1",
      status,
      parameters: { query: "status report", payload: ["alpha"] },
      proxy_location: ProxyLocation.ResidentialDE,
      webhook_callback_url: "https://example.com/webhook",
      max_screenshot_scrolls: 8,
      run_with: "code",
      browser_profile_id: "profile_synthetic",
      task_v2: null,
      workflow: { deleted_at: null },
      ...overrides,
    },
  });
}

function locationState() {
  return JSON.parse(screen.getByTestId("location-state").textContent ?? "null");
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
beforeEach(() => mockRun(Status.Running));

describe("RunStopButton concurrency with a live block run", () => {
  test("a running block run keeps both Stop and Run available", () => {
    renderAt("/workflows/wpid_1/studio?wr=wr_1&bl=Block%201");
    expect(screen.queryByRole("button", { name: /Stop/ })).not.toBeNull();
    expect(screen.queryByRole("button", { name: /Run/ })).not.toBeNull();
  });

  test("starting a full run over a live block run asks for a soft confirm", () => {
    renderAt("/workflows/wpid_1/studio?wr=wr_1&bl=Block%201");

    fireEvent.click(screen.getByRole("button", { name: /Run/ }));
    expect(screen.queryByText("Start a full run?")).not.toBeNull();
    expect(screen.queryByTestId("location")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Start full run" }));
    expect(screen.getByTestId("location").textContent).toBe(
      "/agents/wpid_1/run",
    );
    expect(locationState()).toBeNull();
  });

  test("the confirm can be declined without navigating", () => {
    renderAt("/workflows/wpid_1/studio?wr=wr_1&bl=Block%201");

    fireEvent.click(screen.getByRole("button", { name: /Run/ }));
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));

    expect(screen.queryByTestId("location")).toBeNull();
    expect(screen.queryByRole("button", { name: /Stop/ })).not.toBeNull();
  });

  test("a running full run offers Stop only", () => {
    renderAt("/workflows/wpid_1/studio?wr=wr_1");
    expect(screen.queryByRole("button", { name: /Stop/ })).not.toBeNull();
    expect(screen.queryByRole("button", { name: /Run/ })).toBeNull();
  });

  test("a finalized workflow run reruns with the legacy navigation state", () => {
    mockRun(Status.Completed);
    renderAt("/workflows/wpid_1/studio?wr=wr_1");

    fireEvent.click(screen.getByRole("button", { name: "Re-run agent" }));

    expect(screen.queryByText("Start a full run?")).toBeNull();
    expect(screen.getByTestId("location").textContent).toBe(
      "/agents/wpid_1/run",
    );
    expect(locationState()).toEqual({
      data: { query: "status report", payload: ["alpha"] },
      proxyLocation: ProxyLocation.ResidentialDE,
      webhookCallbackUrl: "https://example.com/webhook",
      maxScreenshotScrolls: 8,
      runWith: "code",
      browserProfileId: "profile_synthetic",
    });
  });

  test("no focused run starts fresh", () => {
    workflowRunQueryMock.mockReturnValue({ data: undefined });
    renderAt("/workflows/wpid_1/studio");

    fireEvent.click(screen.getByRole("button", { name: "Run agent" }));

    expect(screen.getByTestId("location").textContent).toBe(
      "/agents/wpid_1/run",
    );
    expect(locationState()).toBeNull();
  });

  test("a stale prior run (id mismatches the URL) does not seed a rerun", () => {
    // keepPreviousData can surface the previously focused run after the URL run
    // clears/changes; its id no longer matches, so the button stays fresh.
    mockRun(Status.Completed, { workflow_run_id: "wr_previous" });
    renderAt("/workflows/wpid_1/studio?wr=wr_1");

    fireEvent.click(screen.getByRole("button", { name: "Run agent" }));

    expect(screen.getByTestId("location").textContent).toBe(
      "/agents/wpid_1/run",
    );
    expect(locationState()).toBeNull();
  });

  test("a finalized block run starts a fresh full run", () => {
    mockRun(Status.Completed);
    renderAt("/workflows/wpid_1/studio?wr=wr_1&bl=Block%201");

    fireEvent.click(screen.getByRole("button", { name: "Run agent" }));

    expect(screen.getByTestId("location").textContent).toBe(
      "/agents/wpid_1/run",
    );
    expect(locationState()).toBeNull();
  });

  test("a finalized task run starts fresh", () => {
    mockRun(Status.Failed, { task_v2: { task_id: "task_synthetic" } });
    renderAt("/workflows/wpid_1/studio?wr=wr_1");

    fireEvent.click(screen.getByRole("button", { name: "Run agent" }));

    expect(screen.getByTestId("location").textContent).toBe(
      "/agents/wpid_1/run",
    );
    expect(locationState()).toBeNull();
  });

  test("an editor-open layout is NOT carried into the run form (full runs reset)", () => {
    mockRun(Status.Completed);
    renderAt("/workflows/wpid_1/studio?wr=wr_1&panes=editor,copilot");

    fireEvent.click(screen.getByRole("button", { name: "Re-run agent" }));

    expect(screen.getByTestId("location").textContent).toBe(
      "/agents/wpid_1/run",
    );
  });
});

// Global workflows can't start runs from the studio, but recipe pages run
// them in place — a live run must still be stoppable from the top bar.
describe("RunStopButton stopOnly (global workflows)", () => {
  test("a running run offers Stop and no Run", () => {
    renderAt("/workflows/wpid_1/studio?wr=wr_1", <RunStopButton stopOnly />);
    expect(screen.queryByRole("button", { name: /Stop/ })).not.toBeNull();
    expect(screen.queryByRole("button", { name: /Run/ })).toBeNull();
  });

  test("a running block run offers Stop only — no concurrent full run", () => {
    renderAt(
      "/workflows/wpid_1/studio?wr=wr_1&bl=Block%201",
      <RunStopButton stopOnly />,
    );
    expect(screen.queryByRole("button", { name: /Stop/ })).not.toBeNull();
    expect(screen.queryByRole("button", { name: /Run/ })).toBeNull();
  });

  test("stopping asks for the same soft confirm", () => {
    renderAt("/workflows/wpid_1/studio?wr=wr_1", <RunStopButton stopOnly />);

    fireEvent.click(screen.getByRole("button", { name: /Stop/ }));
    expect(screen.queryByText("Stop this run?")).not.toBeNull();
  });

  test("a finished run renders nothing", () => {
    mockRun(Status.Completed);
    renderAt("/workflows/wpid_1/studio?wr=wr_1", <RunStopButton stopOnly />);
    expect(screen.queryByRole("button")).toBeNull();
  });

  test("no focused run renders nothing", () => {
    workflowRunQueryMock.mockReturnValue({ data: undefined });
    renderAt("/workflows/wpid_1/studio", <RunStopButton stopOnly />);
    expect(screen.queryByRole("button")).toBeNull();
  });
});
