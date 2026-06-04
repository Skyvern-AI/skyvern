// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { WorkflowScopeContext } from "@/routes/workflows/editor/WorkflowScopeContext";

import { WorkflowBlockInputSet } from "./WorkflowBlockInputSet";

vi.mock("@/store/WorkflowParametersStore", () => ({
  useWorkflowParametersStore: () => ({ parameters: [{ key: "suffix" }] }),
}));

vi.mock("@/routes/workflows/editor/nodes/WorkflowBlockParameterSelect", () => ({
  WorkflowBlockParameterSelect: () => null,
}));

afterEach(cleanup);

function renderSet(readOnly: boolean) {
  return render(
    <WorkflowScopeContext.Provider value={{ workflowId: "w", readOnly }}>
      <WorkflowBlockInputSet
        nodeId="n1"
        values={new Set(["suffix"])}
        onChange={() => {}}
      />
    </WorkflowScopeContext.Provider>,
  );
}

describe("WorkflowBlockInputSet in a read-only scope", () => {
  test("shows add and remove controls in the live editor scope", () => {
    renderSet(false);
    expect(screen.getByText("suffix")).toBeTruthy();
    expect(screen.queryByTestId("input-set-add")).not.toBeNull();
    expect(screen.queryByTestId("input-set-remove-suffix")).not.toBeNull();
  });

  // The displayed inputs stay readable but cannot be added/removed in a comparison.
  test("hides add and remove controls in a read-only comparison scope", () => {
    renderSet(true);
    expect(screen.getByText("suffix")).toBeTruthy();
    expect(screen.queryByTestId("input-set-add")).toBeNull();
    expect(screen.queryByTestId("input-set-remove-suffix")).toBeNull();
  });
});
