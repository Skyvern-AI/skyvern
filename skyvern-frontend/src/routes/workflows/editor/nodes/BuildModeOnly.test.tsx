// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, test } from "vitest";

import { WorkflowScopeContext } from "../WorkflowScopeContext";
import { BuildModeOnly } from "./BuildModeOnly";

afterEach(cleanup);

function renderAt(
  path: string,
  scope: { workflowId: string | null; readOnly: boolean },
  props: { renderInReadOnlyComparison?: boolean } = {},
) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <WorkflowScopeContext.Provider value={scope}>
        <BuildModeOnly {...props}>
          <div data-testid="child">child</div>
        </BuildModeOnly>
      </WorkflowScopeContext.Provider>
    </MemoryRouter>,
  );
}

describe("BuildModeOnly", () => {
  test("renders children in build mode", () => {
    renderAt("/workflows/wpid_abc/build", { workflowId: "w", readOnly: false });
    expect(screen.queryByTestId("child")).not.toBeNull();
  });

  test("renders nothing in edit mode on the live editor", () => {
    renderAt("/workflows/wpid_abc/edit", { workflowId: "w", readOnly: false });
    expect(screen.queryByTestId("child")).toBeNull();
  });

  // Comparison canvases mount under /edit without a sidebar; the inline form is the only prompt surface.
  test("renders children in edit mode inside a read-only comparison scope", () => {
    renderAt("/workflows/wpid_abc/edit", { workflowId: "w", readOnly: true });
    expect(screen.queryByTestId("child")).not.toBeNull();
  });

  // Non-prompt forms (e.g. workflow settings) opt out so their interactive controls stay out of comparison.
  test("opts out of the read-only comparison scope when asked", () => {
    renderAt(
      "/workflows/wpid_abc/edit",
      { workflowId: "w", readOnly: true },
      { renderInReadOnlyComparison: false },
    );
    expect(screen.queryByTestId("child")).toBeNull();
  });
});
