// @vitest-environment jsdom

vi.mock("@/routes/workflows/components/CodeEditor", () => ({
  CodeEditor: ({
    value,
    extraExtensions,
    lineWrap,
  }: {
    value: string;
    extraExtensions?: Array<unknown>;
    lineWrap?: boolean;
  }) => (
    <div
      data-testid="code-editor"
      data-extension-count={String(extraExtensions?.length ?? 0)}
      data-line-wrap={String(lineWrap ?? true)}
    >
      {value}
    </div>
  ),
}));

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ActionsApiResponse, ActionTypes, Status } from "@/api/types";
import type { WorkflowParameter } from "@/routes/workflows/types/workflowTypes";
import { CodeBlockParameters } from "./CodeBlockParameters";

function buildAction(
  overrides: Partial<ActionsApiResponse> = {},
): ActionsApiResponse {
  return {
    action_id: "wrb_code_action_0",
    action_type: ActionTypes.Click,
    status: Status.Completed,
    task_id: null,
    step_id: null,
    step_order: null,
    action_order: 0,
    confidence_float: null,
    description: null,
    reasoning: null,
    intention: null,
    response: null,
    created_by: null,
    text: null,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
});

function expectInDocumentOrder(titles: Array<string>) {
  const nodes = titles.map((title) => screen.getByText(title, { exact: true }));
  for (let i = 1; i < nodes.length; i++) {
    expect(
      nodes[i - 1]!.compareDocumentPosition(nodes[i]!) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  }
}

const siteUrlParameter = {
  key: "site_url",
  description: null,
  parameter_type: "workflow",
  workflow_parameter_type: "string",
  default_value: null,
} as WorkflowParameter;

const twoSteps = [
  {
    description: "Open Hacker News",
    action_type: "goto_url",
    line_start: 1,
    line_end: 1,
  },
  {
    description: "Read the top title",
    action_type: "extract",
    line_start: 2,
    line_end: 3,
  },
];

describe("CodeBlockParameters", () => {
  it("opens on Plain with the goal and steps, and the Code toggle swaps in the code", () => {
    render(
      <CodeBlockParameters
        code="x = 1"
        prompt="Find the top post"
        steps={twoSteps}
        parameters={[siteUrlParameter]}
        uses={[
          { name: "site_url", kind: "input", found: true, value: "x" },
          { name: "login_output", kind: "block", found: false },
        ]}
        blockStatus={Status.Completed}
        failureReason={null}
        actions={[]}
      />,
    );

    expectInDocumentOrder([
      "Goal",
      "Steps",
      "Inputs",
      "Uses",
      "Upstream outputs",
    ]);
    expect(screen.getByText("Find the top post")).toBeDefined();
    expect(screen.getByText("L2-3")).toBeDefined();
    expect(screen.queryByTestId("code-editor")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /code/i }));

    expect(screen.queryByText("Find the top post")).toBeNull();
    expect(screen.queryByText("Open Hacker News")).toBeNull();
    expect(screen.getByText("Uses")).toBeDefined();
    expect(
      screen.getByTestId("code-editor").getAttribute("data-line-wrap"),
    ).toBe("true");
  });

  it("marks the step containing the failing line", () => {
    render(
      <CodeBlockParameters
        code={"x = 1\nraise ValueError('boom')\ny = 2"}
        prompt="Find the top post"
        steps={twoSteps}
        blockStatus={Status.Failed}
        failureReason="boom"
        actions={[
          buildAction({
            action_type: ActionTypes.NullAction,
            status: Status.Failed,
            response: "boom",
            output: { code_line: 2 },
          }),
        ]}
      />,
    );

    expect(screen.getByText("Failed at line 2")).toBeDefined();
    const marked = document.querySelectorAll('[data-failing="true"]');
    expect(marked).toHaveLength(1);
    expect(marked[0]!.textContent).toContain("Read the top title");
  });

  it("wires the jinja highlight into the code editor", () => {
    render(
      <CodeBlockParameters
        code="print('{{ name }}')"
        blockStatus={Status.Completed}
        failureReason={null}
        actions={[]}
      />,
    );

    expect(
      screen.getByTestId("code-editor").getAttribute("data-extension-count"),
    ).toBe("3");
  });

  it("highlights the failing line in the code editor", () => {
    render(
      <CodeBlockParameters
        code={"x = 1\nraise ValueError('boom')"}
        blockStatus={Status.Failed}
        failureReason="boom"
        actions={[
          buildAction({
            action_type: ActionTypes.NullAction,
            status: Status.Failed,
            response: "boom",
            output: { code_line: 2 },
          }),
        ]}
      />,
    );

    // jinja (2) + wrapped-line indent + error lineHighlight field + theme (2) = 5.
    expect(
      screen.getByTestId("code-editor").getAttribute("data-extension-count"),
    ).toBe("5");
  });

  it("renders a failing line callout when the block failed and an action carries a code line", () => {
    render(
      <CodeBlockParameters
        code={"x = 1\nraise ValueError('boom')"}
        blockStatus={Status.Failed}
        failureReason="division by zero"
        actions={[
          buildAction({
            action_id: "wrb_code_action_1",
            action_type: ActionTypes.NullAction,
            status: Status.Failed,
            response: "boom",
            output: { code_line: 7 },
          }),
          buildAction({
            action_id: "wrb_code_action_0",
            output: { code_line: 2, duration_ms: 120 },
          }),
        ]}
      />,
    );

    expect(
      screen.getByText(/Failed at line 7: division by zero/),
    ).toBeDefined();
  });

  it("falls back to the failing action response when the block has no failure reason", () => {
    render(
      <CodeBlockParameters
        code="raise ValueError('boom')"
        blockStatus={Status.Failed}
        failureReason={null}
        actions={[
          buildAction({
            action_type: ActionTypes.NullAction,
            status: Status.Failed,
            response: "ValueError: boom",
            output: { code_line: 1 },
          }),
        ]}
      />,
    );

    expect(
      screen.getByText(/Failed at line 1: ValueError: boom/),
    ).toBeDefined();
  });

  it("renders no callout when the block completed", () => {
    render(
      <CodeBlockParameters
        code="x = 1"
        blockStatus={Status.Completed}
        failureReason={null}
        actions={[buildAction({ output: { code_line: 1, duration_ms: 50 } })]}
      />,
    );

    expect(screen.queryByText(/Failed at line/)).toBeNull();
  });

  it("renders no callout when no failed action carries a code line", () => {
    render(
      <CodeBlockParameters
        code="x = 1"
        blockStatus={Status.Failed}
        failureReason="timed out"
        actions={[buildAction({ status: Status.Failed, output: null })]}
      />,
    );

    expect(screen.queryByText(/Failed at line/)).toBeNull();
  });
});
