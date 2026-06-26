// @vitest-environment jsdom

vi.mock("@/routes/workflows/components/CodeEditor", () => ({
  CodeEditor: ({
    value,
    extraExtensions,
  }: {
    value: string;
    extraExtensions?: Array<unknown>;
  }) => (
    <div
      data-testid="code-editor"
      data-extension-count={String(extraExtensions?.length ?? 0)}
    >
      {value}
    </div>
  ),
}));

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ActionsApiResponse, ActionTypes, Status } from "@/api/types";
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

describe("CodeBlockParameters", () => {
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
    ).toBe("2");
  });

  it("surfaces the goal and steps when provided", () => {
    render(
      <CodeBlockParameters
        code="x = 1"
        prompt="Find the top post"
        steps={[
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
        ]}
        blockStatus={Status.Completed}
        failureReason={null}
        actions={[]}
      />,
    );

    expect(screen.getByText("Find the top post")).toBeDefined();
    expect(screen.getByText("Open Hacker News")).toBeDefined();
    expect(screen.getByText("L2-3")).toBeDefined();
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

    // jinja (2) + error lineHighlight field + theme (2) = 4.
    expect(
      screen.getByTestId("code-editor").getAttribute("data-extension-count"),
    ).toBe("4");
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
