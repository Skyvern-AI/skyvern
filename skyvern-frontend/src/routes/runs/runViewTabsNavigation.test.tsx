// @vitest-environment jsdom

import { render, screen, fireEvent } from "@testing-library/react";
import {
  createMemoryRouter,
  Navigate,
  Outlet,
  Route,
  Routes,
  RouterProvider,
  useParams,
} from "react-router-dom";
import { describe, expect, test } from "vitest";

import {
  SwitchBarNavigation,
  type SwitchBarNavigationOption,
} from "@/components/SwitchBarNavigation";

import { runViewTabBasePath } from "./runViewTabBasePath";

type Tabs = readonly [string, ...string[]];

function TabLayout({ tabs }: { tabs: Tabs }) {
  const base = runViewTabBasePath(useParams());
  const options: SwitchBarNavigationOption[] = tabs.map((tab) => ({
    label: tab,
    to: `${base}/${tab}`,
  }));
  return (
    <div>
      <SwitchBarNavigation options={options} />
      <Outlet />
    </div>
  );
}

function leafRoutes(tabs: Tabs) {
  return [
    { index: true, element: <Navigate to={tabs[0]} replace /> },
    ...tabs.map((tab) => ({ path: tab, element: <div>{`PANEL_${tab}`}</div> })),
  ];
}

function RunsSplat({ tabs }: { tabs: Tabs }) {
  return (
    <Routes>
      <Route element={<TabLayout tabs={tabs} />}>
        <Route index element={<Navigate to={tabs[0]} replace />} />
        {tabs.map((tab) => (
          <Route key={tab} path={tab} element={<div>{`PANEL_${tab}`}</div>} />
        ))}
      </Route>
    </Routes>
  );
}

function renderRouter(
  routes: Parameters<typeof createMemoryRouter>[0],
  entry: string,
) {
  return render(
    <RouterProvider
      router={createMemoryRouter(routes, { initialEntries: [entry] })}
    />,
  );
}

function hrefPath(name: string): string {
  const href = screen.getByRole("link", { name }).getAttribute("href");
  return new URL(href ?? "", "http://localhost").pathname;
}

function hrefSearch(name: string): string {
  const href = screen.getByRole("link", { name }).getAttribute("href");
  return new URL(href ?? "", "http://localhost").search;
}

function ariaCurrent(name: string): string | null {
  return screen.getByRole("link", { name }).getAttribute("aria-current");
}

const WF_TABS = [
  "overview",
  "output",
  "parameters",
  "recording",
  "code",
] as const;
const TASK_TABS = [
  "actions",
  "recording",
  "parameters",
  "diagnostics",
] as const;

describe("run view tab navigation stays in its mounted route family", () => {
  test("agent long-form route keeps /agents/{wpid}/{wrid} and the active tab", async () => {
    renderRouter(
      [
        {
          path: "agents/:workflowPermanentId/:workflowRunId",
          element: <TabLayout tabs={WF_TABS} />,
          children: leafRoutes(WF_TABS),
        },
      ],
      "/agents/wpid_1/wr_1/overview?active=act_1",
    );
    expect(hrefPath("output")).toBe("/agents/wpid_1/wr_1/output");
    expect(hrefSearch("output")).toBe("?active=act_1");
    expect(ariaCurrent("overview")).toBe("page");
    expect(ariaCurrent("output")).toBeNull();

    fireEvent.click(screen.getByRole("link", { name: "output" }));
    expect(await screen.findByText("PANEL_output")).toBeTruthy();
    expect(hrefPath("output")).toBe("/agents/wpid_1/wr_1/output");
    expect(ariaCurrent("output")).toBe("page");
  });

  test("task route keeps /tasks/{taskId} and the active tab", async () => {
    renderRouter(
      [
        {
          path: "tasks/:taskId",
          element: <TabLayout tabs={TASK_TABS} />,
          children: leafRoutes(TASK_TABS),
        },
      ],
      "/tasks/tsk_1/actions?active=act_1",
    );
    expect(hrefPath("recording")).toBe("/tasks/tsk_1/recording");
    expect(hrefSearch("recording")).toBe("?active=act_1");
    expect(ariaCurrent("actions")).toBe("page");

    fireEvent.click(screen.getByRole("link", { name: "recording" }));
    expect(await screen.findByText("PANEL_recording")).toBeTruthy();
    expect(hrefPath("recording")).toBe("/tasks/tsk_1/recording");
  });

  test("runs splat keeps /runs/{runId} and renders the panel on click", async () => {
    renderRouter(
      [{ path: "runs/:runId/*", element: <RunsSplat tabs={WF_TABS} /> }],
      "/runs/wr_1/overview?active=act_1",
    );
    expect(hrefPath("output")).toBe("/runs/wr_1/output");
    expect(hrefSearch("output")).toBe("?active=act_1");
    expect(ariaCurrent("overview")).toBe("page");

    fireEvent.click(screen.getByRole("link", { name: "output" }));
    expect(await screen.findByText("PANEL_output")).toBeTruthy();
  });

  test("a relative target under the runs splat misroutes against the matched segment", () => {
    function RelativeSplat() {
      return (
        <Routes>
          <Route index element={<Navigate to="overview" replace />} />
          <Route
            path="overview"
            element={
              <div>
                <SwitchBarNavigation
                  options={[{ label: "output", to: "output" }]}
                />
                <div>PANEL_overview</div>
              </div>
            }
          />
          <Route path="output" element={<div>PANEL_output</div>} />
        </Routes>
      );
    }
    renderRouter(
      [{ path: "runs/:runId/*", element: <RelativeSplat /> }],
      "/runs/wr_1/overview?active=act_1",
    );
    expect(hrefPath("output")).toBe("/runs/wr_1/overview/output");
  });
});
