// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { ProxyLocation, Status } from "@/api/types";
import { RunTab } from "./RunTab";

const { runsQueryMock, workflowRunQueryMock } = vi.hoisted(() => ({
  runsQueryMock: vi.fn(),
  workflowRunQueryMock: vi.fn(),
}));

vi.mock("../hooks/useWorkflowRunsQuery", () => ({
  useWorkflowRunsQuery: () => runsQueryMock(),
}));
vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: (options?: { workflowRunId?: string }) =>
    workflowRunQueryMock(options),
}));

vi.mock("./runview/RunView", () => ({
  RunView: (props: { onRetry?: () => void; runIdPending?: boolean }) => (
    <div
      data-testid="runview"
      data-has-retry={props.onRetry ? "yes" : "no"}
      data-run-id-pending={props.runIdPending ? "yes" : "no"}
    >
      {props.onRetry ? (
        <button type="button" onClick={props.onRetry}>
          Retry as-is
        </button>
      ) : null}
    </div>
  ),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
beforeEach(() => {
  runsQueryMock.mockReturnValue({ data: [], isPending: false });
  workflowRunQueryMock.mockReturnValue({ data: undefined });
});

function mockWorkflowRun(overrides: Record<string, unknown> = {}) {
  workflowRunQueryMock.mockReturnValue({
    data: {
      workflow_run_id: "run_1",
      status: Status.Failed,
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

function LocationProbe() {
  const location = useLocation();
  return (
    <>
      <div data-testid="location">{location.pathname}</div>
      <div data-testid="location-state">
        {JSON.stringify(location.state ?? null)}
      </div>
    </>
  );
}

function locationState() {
  return JSON.parse(screen.getByTestId("location-state").textContent ?? "null");
}

// useStudioRunId reads ?wr= from the router, so the MemoryRouter URL drives it
// (and ?bl=, which RunTab reads directly) — no hook mock needed.
function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route
          path="/workflows/:workflowPermanentId/studio"
          element={<RunTab />}
        />
        <Route
          path="/agents/:workflowPermanentId/run"
          element={<LocationProbe />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe("RunTab block-scoped retry", () => {
  test("suppresses the retry CTA for a block run (?bl= present)", () => {
    renderAt("/workflows/wpid_abc/studio?wr=run_1&bl=Block%201");
    expect(screen.getByTestId("runview").getAttribute("data-has-retry")).toBe(
      "no",
    );
  });

  test("wires the retry CTA for a full run (no ?bl=)", () => {
    renderAt("/workflows/wpid_abc/studio?wr=run_1");
    expect(screen.getByTestId("runview").getAttribute("data-has-retry")).toBe(
      "yes",
    );
  });
});

describe("RunTab retry navigation", () => {
  test("an explicit failed workflow run retries with the legacy state", () => {
    mockWorkflowRun();
    renderAt("/workflows/wpid_abc/studio?wr=run_1");

    fireEvent.click(screen.getByRole("button", { name: "Retry as-is" }));

    expect(screen.getByTestId("location").textContent).toBe(
      "/agents/wpid_abc/run",
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

  test("the latest-run fallback retries the inspected run with state", () => {
    runsQueryMock.mockReturnValue({
      data: [{ workflow_run_id: "run_latest" }],
      isPending: false,
    });
    mockWorkflowRun({ workflow_run_id: "run_latest" });
    renderAt("/workflows/wpid_abc/studio");

    fireEvent.click(screen.getByRole("button", { name: "Retry as-is" }));

    expect(workflowRunQueryMock).toHaveBeenCalledWith({
      workflowRunId: "run_latest",
    });
    expect(locationState()).toEqual({
      data: { query: "status report", payload: ["alpha"] },
      proxyLocation: ProxyLocation.ResidentialDE,
      webhookCallbackUrl: "https://example.com/webhook",
      maxScreenshotScrolls: 8,
      runWith: "code",
      browserProfileId: "profile_synthetic",
    });
  });

  test("a task run keeps the existing state-less retry", () => {
    mockWorkflowRun({ task_v2: { task_id: "task_synthetic" } });
    renderAt("/workflows/wpid_abc/studio?wr=run_1");

    fireEvent.click(screen.getByRole("button", { name: "Retry as-is" }));

    expect(screen.getByTestId("location").textContent).toBe(
      "/agents/wpid_abc/run",
    );
    expect(locationState()).toBeNull();
  });
});

describe("RunTab run-id resolution", () => {
  test("marks the run id pending while the recent-runs query is loading", () => {
    runsQueryMock.mockReturnValue({ data: undefined, isPending: true });
    renderAt("/workflows/wpid_abc/studio");
    expect(
      screen.getByTestId("runview").getAttribute("data-run-id-pending"),
    ).toBe("yes");
  });

  test("does not mark pending once the recent-runs query settles empty", () => {
    renderAt("/workflows/wpid_abc/studio");
    expect(
      screen.getByTestId("runview").getAttribute("data-run-id-pending"),
    ).toBe("no");
  });

  test("does not mark pending when the run id comes from the URL", () => {
    runsQueryMock.mockReturnValue({ data: undefined, isPending: true });
    renderAt("/workflows/wpid_abc/studio?wr=run_1");
    expect(
      screen.getByTestId("runview").getAttribute("data-run-id-pending"),
    ).toBe("no");
  });

  test("marks the run id pending while the globalWorkflows prerequisite is still loading (query disabled)", () => {
    runsQueryMock.mockReturnValue({
      data: undefined,
      isPending: true,
      isLoading: false,
    });
    renderAt("/workflows/wpid_abc/studio");
    expect(
      screen.getByTestId("runview").getAttribute("data-run-id-pending"),
    ).toBe("yes");
  });
});
