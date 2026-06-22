// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";
import { afterEach, describe, expect, test } from "vitest";

import { WorkflowScopeContext } from "@/routes/workflows/editor/WorkflowScopeContext";

import { WorkflowBlockInput } from "./WorkflowBlockInput";

afterEach(cleanup);

function renderInput(readOnly: boolean) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ReactFlowProvider>
        <WorkflowScopeContext.Provider value={{ workflowId: "w", readOnly }}>
          <WorkflowBlockInput
            nodeId="n1"
            value="suffix.csv"
            onChange={() => {}}
          />
        </WorkflowScopeContext.Provider>
      </ReactFlowProvider>
    </QueryClientProvider>,
  );
}

describe("WorkflowBlockInput in a read-only scope", () => {
  test("stays editable with the parameter action in the live editor scope", () => {
    renderInput(false);
    const input = screen.getByRole("textbox") as HTMLInputElement;
    expect(input.readOnly).toBe(false);
    expect(screen.queryByTestId("block-input-actions")).not.toBeNull();
  });

  // Comparison canvases need the value readable (still in the a11y tree) but not editable.
  test("is readOnly and hides actions in a read-only comparison scope", () => {
    renderInput(true);
    const input = screen.getByRole("textbox") as HTMLInputElement;
    expect(input.readOnly).toBe(true);
    expect(screen.queryByTestId("block-input-actions")).toBeNull();
  });
});
