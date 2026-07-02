// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { Status } from "@/api/types";

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
  return <div data-testid="location">{location.pathname}</div>;
}

function renderAt(path: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route
            path="/workflows/:workflowPermanentId/studio"
            element={<RunStopButton />}
          />
          <Route
            path="/workflows/:workflowPermanentId/run"
            element={<LocationProbe />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function mockRun(status: Status) {
  workflowRunQueryMock.mockReturnValue({
    data: { workflow_run_id: "wr_1", status },
  });
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
      "/workflows/wpid_1/run",
    );
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

  test("an idle workflow starts a run directly, with no confirm", () => {
    mockRun(Status.Completed);
    renderAt("/workflows/wpid_1/studio?wr=wr_1");

    fireEvent.click(screen.getByRole("button", { name: /Run/ }));

    expect(screen.queryByText("Start a full run?")).toBeNull();
    expect(screen.getByTestId("location").textContent).toBe(
      "/workflows/wpid_1/run",
    );
  });
});
