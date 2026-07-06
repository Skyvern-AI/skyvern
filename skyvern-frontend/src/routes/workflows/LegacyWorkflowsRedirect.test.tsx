import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { BuildRoute, DebugRoute } from "./StudioRouteGates";
import { LegacyWorkflowsRedirect } from "./LegacyWorkflowsRedirect";

const studioState = { enabled: true };

vi.mock("@/hooks/useWorkflowStudioEnabled", () => ({
  useWorkflowStudioEnabled: () => studioState.enabled,
}));

vi.mock("./debugger/Debugger", () => ({
  Debugger: () => <div>debugger</div>,
}));

vi.mock("./editor/WorkflowEditor", () => ({
  WorkflowEditor: () => <div>editor</div>,
}));

function LocationProbe() {
  const location = useLocation();
  return (
    <>
      <div data-testid="location">
        {location.pathname}
        {location.search}
        {location.hash}
      </div>
      <div data-testid="state">{JSON.stringify(location.state)}</div>
    </>
  );
}

function renderAt(initialEntry: string | { pathname: string; state: unknown }) {
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/workflows/*" element={<LegacyWorkflowsRedirect />} />
        <Route
          path="/agents/:workflowPermanentId/build"
          element={<BuildRoute />}
        />
        <Route
          path="/agents/:workflowPermanentId/:workflowRunId/:blockLabel/build"
          element={<BuildRoute />}
        />
        <Route
          path="/agents/:workflowPermanentId/debug"
          element={<DebugRoute />}
        />
        <Route path="*" element={null} />
      </Routes>
      <LocationProbe />
    </MemoryRouter>,
  );
}

describe("LegacyWorkflowsRedirect", () => {
  it("redirects the bare list URL", async () => {
    renderAt("/workflows");
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe("/agents"),
    );
  });

  it("preserves the sub-path, query params, and hash byte-for-byte", async () => {
    renderAt(
      "/workflows/wpid_1/wr_2/overview?active=act_1&iteration=2&cache-key-value=a%20b&panes=copilot,browser#frame",
    );
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe(
        "/agents/wpid_1/wr_2/overview?active=act_1&iteration=2&cache-key-value=a%20b&panes=copilot,browser#frame",
      ),
    );
  });

  it("keeps encoded path segments (spaces, unicode, %2F) untouched", async () => {
    renderAt("/workflows/wpid_1/scripts/St%C3%A4dte%20a%2Fb");
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe(
        "/agents/wpid_1/scripts/St%C3%A4dte%20a%2Fb",
      ),
    );
  });

  it("carries location.state through the redirect", async () => {
    renderAt({ pathname: "/workflows/wpid_1/run", state: { data: { a: 1 } } });
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe(
        "/agents/wpid_1/run",
      ),
    );
    expect(screen.getByTestId("state").textContent).toBe(
      JSON.stringify({ data: { a: 1 } }),
    );
  });

  it("composes with the studio gate: legacy block-build URL lands on /agents studio with re-encoded ?bl=", async () => {
    studioState.enabled = true;
    renderAt("/workflows/wpid_1/wr_9/St%C3%A4dte%20suchen/build");
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe(
        "/agents/wpid_1/studio?wr=wr_9&bl=St%C3%A4dte%20suchen",
      ),
    );
  });

  it("composes with the studio gate: legacy /build keeps its own query when studio is on", async () => {
    studioState.enabled = true;
    renderAt("/workflows/wpid_1/build?via=discover");
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe(
        "/agents/wpid_1/studio?via=discover",
      ),
    );
  });

  it("composes the 3-hop chain: legacy /debug → /agents/debug → /agents/build when studio is off", async () => {
    studioState.enabled = false;
    renderAt("/workflows/wpid_1/debug?wr=wr_1#frame");
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe(
        "/agents/wpid_1/build?wr=wr_1#frame",
      ),
    );
  });

  it("stays on /agents build (no further hop) when studio is off", async () => {
    studioState.enabled = false;
    renderAt("/workflows/wpid_1/build?wr=wr_1");
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe(
        "/agents/wpid_1/build?wr=wr_1",
      ),
    );
    expect(screen.getByText("debugger")).toBeTruthy();
  });
});
