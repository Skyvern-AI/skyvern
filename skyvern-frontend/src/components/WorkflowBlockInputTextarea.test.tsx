// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";
import { afterEach, describe, expect, test } from "vitest";

import { WorkflowScopeContext } from "@/routes/workflows/editor/WorkflowScopeContext";

import { WorkflowBlockInputTextarea } from "./WorkflowBlockInputTextarea";

afterEach(cleanup);

function renderTextarea(readOnly: boolean) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ReactFlowProvider>
        <WorkflowScopeContext.Provider value={{ workflowId: "w", readOnly }}>
          <WorkflowBlockInputTextarea
            nodeId="n1"
            value="goal text"
            aiImprove={{ useCase: "navigation" }}
            onChange={() => {}}
          />
        </WorkflowScopeContext.Provider>
      </ReactFlowProvider>
    </QueryClientProvider>,
  );
}

describe("WorkflowBlockInputTextarea in a read-only scope", () => {
  test("stays editable with actions in the live editor scope", () => {
    renderTextarea(false);
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(textarea.readOnly).toBe(false);
    expect(screen.queryByTestId("block-textarea-actions")).not.toBeNull();
  });

  // Comparison canvases need the prompt readable (still in the a11y tree) but not editable, and no prompt-improve action.
  test("is readOnly and hides actions in a read-only comparison scope", () => {
    renderTextarea(true);
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(textarea.readOnly).toBe(true);
    expect(screen.queryByTestId("block-textarea-actions")).toBeNull();
  });
});
