// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { describe, expect, test, vi } from "vitest";

import { StudioPaneDefaultsProvider } from "./studio/StudioPaneDefaults";
import { useStudioPanes } from "./studio/useStudioPanes";
import { WorkflowRunRoute } from "./StudioRouteGates";

vi.mock("./editor/WorkflowEditor", () => ({
  WorkflowEditor: () => <div data-testid="workflow-editor" />,
}));

vi.mock("./LegacyBuildRedirect", () => ({
  LegacyBuildRedirect: () => <div data-testid="legacy-build-redirect" />,
}));

function LocationProbe() {
  const location = useLocation();
  const { panes } = useStudioPanes();
  return (
    <>
      <output data-testid="panes">{panes.join(",")}</output>
      <div data-testid="location">
        {location.pathname + location.search + location.hash}
      </div>
    </>
  );
}

describe("WorkflowRunRoute", () => {
  test("redirects legacy per-agent run URLs to the short run route", () => {
    render(
      <MemoryRouter
        initialEntries={[
          "/agents/wpid_1/wr_1/overview?active=act_1&iteration=2#frame",
        ]}
      >
        <Routes>
          <Route
            path="/agents/:workflowPermanentId/:workflowRunId/*"
            element={<WorkflowRunRoute />}
          />
          <Route
            path="/runs/:runId"
            element={
              <StudioPaneDefaultsProvider hasBlocks={true}>
                <LocationProbe />
              </StudioPaneDefaultsProvider>
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByTestId("location").textContent).toBe(
      "/runs/wr_1?active=act_1&iteration=2&view=timeline#frame",
    );
  });

  test.each([
    ["blocks", "timeline", "overview,browser"],
    ["output", "timeline", "overview,browser"],
    ["parameters", "timeline", "overview,browser"],
    ["recording", "recording", "browser,overview"],
    ["code", "code", "overview,browser"],
  ])("preserves the legacy %s subview", (legacySubview, studioView, panes) => {
    render(
      <MemoryRouter
        initialEntries={[
          `/agents/wpid_1/wr_1/${legacySubview}?active=act_1#frame`,
        ]}
      >
        <Routes>
          <Route
            path="/agents/:workflowPermanentId/:workflowRunId/*"
            element={<WorkflowRunRoute />}
          />
          <Route
            path="/runs/:runId"
            element={
              <StudioPaneDefaultsProvider hasBlocks={true}>
                <LocationProbe />
              </StudioPaneDefaultsProvider>
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByTestId("location").textContent).toBe(
      `/runs/wr_1?active=act_1&view=${studioView}#frame`,
    );
    expect(screen.getByTestId("panes").textContent).toBe(panes);
  });

  test.each([
    ["code", "browser", "overview,browser"],
    ["recording", "overview", "browser,overview"],
  ])(
    "opens the pane needed for %s while preserving the explicit URL",
    (legacySubview, initialPanes, expectedPanes) => {
      render(
        <MemoryRouter
          initialEntries={[
            `/agents/wpid_1/wr_1/${legacySubview}?panes=${initialPanes}`,
          ]}
        >
          <Routes>
            <Route
              path="/agents/:workflowPermanentId/:workflowRunId/*"
              element={<WorkflowRunRoute />}
            />
            <Route
              path="/runs/:runId"
              element={
                <StudioPaneDefaultsProvider hasBlocks={true}>
                  <LocationProbe />
                </StudioPaneDefaultsProvider>
              }
            />
          </Routes>
        </MemoryRouter>,
      );

      expect(screen.getByTestId("location").textContent).toContain(
        `panes=${initialPanes}`,
      );
      expect(screen.getByTestId("panes").textContent).toBe(expectedPanes);
    },
  );

  test.each([
    ["output", "outputs"],
    ["parameters", "inputs"],
  ])(
    "preserves the run-level legacy %s subview",
    (legacySubview, studioView) => {
      render(
        <MemoryRouter initialEntries={[`/agents/wpid_1/wr_1/${legacySubview}`]}>
          <Routes>
            <Route
              path="/agents/:workflowPermanentId/:workflowRunId/*"
              element={<WorkflowRunRoute />}
            />
            <Route
              path="/runs/:runId"
              element={
                <StudioPaneDefaultsProvider hasBlocks={true}>
                  <LocationProbe />
                </StudioPaneDefaultsProvider>
              }
            />
          </Routes>
        </MemoryRouter>,
      );

      expect(screen.getByTestId("location").textContent).toBe(
        `/runs/wr_1?view=${studioView}`,
      );
    },
  );

  test("keeps embedded legacy links focused on their requested pane", () => {
    render(
      <MemoryRouter
        initialEntries={["/agents/wpid_1/wr_1/code?embed=true&panes=browser"]}
      >
        <Routes>
          <Route
            path="/agents/:workflowPermanentId/:workflowRunId/*"
            element={<WorkflowRunRoute />}
          />
          <Route
            path="/runs/:runId"
            element={
              <StudioPaneDefaultsProvider hasBlocks={true}>
                <LocationProbe />
              </StudioPaneDefaultsProvider>
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByTestId("location").textContent).toBe(
      "/runs/wr_1?embed=true&panes=browser&view=code",
    );
    expect(screen.getByTestId("panes").textContent).toBe("overview");
  });
});
