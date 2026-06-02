// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { WorkflowScopeContext } from "@/routes/workflows/editor/WorkflowScopeContext";

import { CodeBlockEditor } from "./CodeBlockEditor";

const node = {
  id: "cb1",
  type: "codeBlock",
  data: { editable: true, code: "print(1)", parameterKeys: [] },
};

vi.mock("@xyflow/react", () => ({
  useReactFlow: () => ({ getNode: () => node, updateNodeData: vi.fn() }),
}));

vi.mock("..", () => ({
  isWorkflowBlockNode: () => true,
}));

vi.mock("@/components/WorkflowBlockInputSet", () => ({
  WorkflowBlockInputSet: () => null,
}));

vi.mock("@/routes/workflows/components/CodeEditor", () => ({
  CodeEditor: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="code-editor" data-readonly={String(Boolean(readOnly))} />
  ),
}));

afterEach(cleanup);

function renderEditor(readOnly: boolean) {
  return render(
    <WorkflowScopeContext.Provider value={{ workflowId: "w", readOnly }}>
      <CodeBlockEditor blockId="cb1" />
    </WorkflowScopeContext.Provider>,
  );
}

describe("CodeBlockEditor in a read-only scope", () => {
  test("keeps the code editor editable in the live editor scope", () => {
    renderEditor(false);

    expect(
      screen.getByTestId("code-editor").getAttribute("data-readonly"),
    ).toBe("false");
  });

  // CodeMirror buffers edits locally, so the displayed historical code must be read-only here.
  test("renders the code editor read-only in a read-only comparison scope", () => {
    renderEditor(true);

    expect(
      screen.getByTestId("code-editor").getAttribute("data-readonly"),
    ).toBe("true");
  });
});
