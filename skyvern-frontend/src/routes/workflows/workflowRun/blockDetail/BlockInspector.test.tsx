// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ActionTypes, Status, type ActionsApiResponse } from "@/api/types";
import type { WorkflowRunBlock } from "../../types/workflowRunTypes";
import { BlockInspector, JsonExplorer } from "./BlockInspector";

const { copyTextMock } = vi.hoisted(() => ({ copyTextMock: vi.fn() }));

vi.mock("@/util/copyText", () => ({ copyText: copyTextMock }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function buildBlock(
  overrides: Partial<WorkflowRunBlock> = {},
): WorkflowRunBlock {
  return {
    workflow_run_block_id: "wrb_default",
    workflow_run_id: "wr_default",
    parent_workflow_run_block_id: null,
    block_type: "file_download",
    label: null,
    description: null,
    title: null,
    status: Status.Failed,
    failure_reason: null,
    output: null,
    continue_on_failure: false,
    task_id: null,
    url: null,
    navigation_goal: null,
    navigation_payload: null,
    data_extraction_goal: null,
    data_schema: null,
    terminate_criterion: null,
    complete_criterion: null,
    include_action_history_in_verification: null,
    engine: null,
    actions: null,
    created_at: "2026-01-01T00:00:00Z",
    modified_at: "2026-01-01T00:00:00Z",
    duration: null,
    loop_values: null,
    current_value: null,
    current_index: null,
    ...overrides,
  };
}

function buildAction(
  overrides: Partial<ActionsApiResponse> = {},
): ActionsApiResponse {
  return {
    action_id: "act_default",
    action_type: ActionTypes.Click,
    status: Status.Completed,
    task_id: null,
    step_id: null,
    step_order: null,
    action_order: null,
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

describe("JsonExplorer", () => {
  it("previews nested objects with key/value content instead of object counts", () => {
    render(
      <JsonExplorer
        rootLabel="output"
        value={[
          [
            {
              loop_value: "/runs",
              output_parameter: { name: "run_id" },
              output_value: { id: "wr_1" },
            },
          ],
        ]}
      />,
    );

    expect(screen.queryByText(/Object\(/)).toBeNull();
    expect(screen.getByText(/loop_value: "\/runs"/)).toBeDefined();
    expect(
      screen.getByText(/output_parameter: \{ name: "run_id" \}/),
    ).toBeDefined();
  });

  it("hides compact previews once an expandable section is open", () => {
    render(
      <JsonExplorer
        rootLabel="output"
        value={[
          {
            loop_value: "/runs",
            output_parameter: { name: "run_id" },
          },
        ]}
      />,
    );

    const row = screen.getByRole("button", {
      name: /0.*loop_value.*\/runs/i,
    });
    expect(row.textContent).toContain('loop_value: "/runs"');

    fireEvent.click(row);

    expect(row.textContent).not.toContain('loop_value: "/runs"');
    expect(screen.getByText("loop_value")).toBeDefined();
  });

  it("offers a copy button per node: raw text for leaves, the full subtree for nested nodes", () => {
    render(
      <JsonExplorer
        rootLabel="output"
        value={{ task: { id: "tsk_1" }, status: "completed" }}
      />,
    );

    expect(screen.getAllByRole("button", { name: /^Copy / })).toHaveLength(3);

    fireEvent.click(screen.getByRole("button", { name: "Copy status" }));
    expect(copyTextMock).toHaveBeenLastCalledWith("completed");

    fireEvent.click(screen.getByRole("button", { name: "Copy task" }));
    expect(copyTextMock).toHaveBeenLastCalledWith(
      JSON.stringify({ id: "tsk_1" }, null, 2),
    );
    // Copying a collapsed node must not expand it.
    expect(screen.queryByText("id")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Copy output" }));
    expect(copyTextMock).toHaveBeenLastCalledWith(
      JSON.stringify({ task: { id: "tsk_1" }, status: "completed" }, null, 2),
    );
  });
});

describe("BlockInspector Inputs tab", () => {
  it("surfaces the block's navigation goal even when a click action is selected", () => {
    const block = buildBlock({
      navigation_goal: "Download the latest invoice PDF",
    });
    const action = buildAction({ action_type: ActionTypes.Click, text: null });

    render(<BlockInspector block={block} action={action} />);
    // Radix Tabs activates on mousedown, not a bare click event.
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Inputs" }));

    expect(screen.getByText("Navigation goal")).toBeDefined();
    expect(screen.getByText("Download the latest invoice PDF")).toBeDefined();
  });

  it("keeps the action's own input alongside block context", () => {
    const block = buildBlock({ navigation_goal: "Fill out the form" });
    const action = buildAction({
      action_type: ActionTypes.InputText,
      text: "user@example.com",
    });

    render(<BlockInspector block={block} action={action} />);
    // Radix Tabs activates on mousedown, not a bare click event.
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Inputs" }));

    expect(screen.getByText("Input")).toBeDefined();
    expect(screen.getByText("user@example.com")).toBeDefined();
    expect(screen.getByText("Navigation goal")).toBeDefined();
    expect(screen.getByText("Fill out the form")).toBeDefined();
  });
});

describe("BlockInspector failure scoping", () => {
  it("shows the failure reason on Summary and not on the other sub-panels", () => {
    const block = buildBlock({
      failure_reason: "Element not found",
    });
    render(<BlockInspector block={block} />);

    expect(screen.getByText("Failure")).toBeDefined();
    expect(screen.getByText("Element not found")).toBeDefined();

    fireEvent.mouseDown(screen.getByRole("tab", { name: "Inputs" }));
    expect(screen.queryByText("Element not found")).toBeNull();
  });

  it("opens on Summary for a failed block even when output exists", () => {
    const block = buildBlock({
      failure_reason: "Element not found",
      output: { result: "partial" },
    });
    render(<BlockInspector block={block} />);

    // Default tab is Summary (not Outputs), so the failure is visible on load.
    expect(screen.getByText("Element not found")).toBeDefined();
  });

  it("surfaces the block failure on Summary when an action is selected", () => {
    const block = buildBlock({ failure_reason: "Login rejected" });
    const action = buildAction({ action_type: ActionTypes.Click });
    render(<BlockInspector block={block} action={action} />);

    expect(screen.getByText("Login rejected")).toBeDefined();
  });

  it("leads a code block failure with its error state, line and code", () => {
    const block = buildBlock({
      block_type: "code",
      error_codes: ["user_code_error"],
      failure_reason:
        "CodeBlock failed with NameError at line 6: name 'min' is not defined.",
    });
    render(<BlockInspector block={block} />);

    expect(screen.getByText("The block's code raised NameError")).toBeDefined();
    expect(screen.getByText("Line 6")).toBeDefined();
    expect(screen.getByText("Error code user_code_error")).toBeDefined();
    const technicalDetails = screen
      .getByText("Technical details")
      .closest("details");
    expect(technicalDetails?.open).toBe(false);
    fireEvent.click(screen.getByText("Technical details"));
    expect(technicalDetails?.open).toBe(true);
    // The raw reason stays: it carries the message the exception name cannot.
    expect(
      screen.getByText(
        "CodeBlock failed with NameError at line 6: name 'min' is not defined.",
      ),
    ).toBeDefined();
  });

  it("tells the user a sandbox fault is not theirs to fix", () => {
    const block = buildBlock({
      block_type: "code",
      error_codes: ["runner_unavailable"],
      failure_reason: "Secure CodeBlock runner is unavailable. Please retry.",
    });
    render(<BlockInspector block={block} />);

    expect(screen.getByText("The code sandbox was unreachable")).toBeDefined();
    expect(screen.getByText(/not a problem with your code/i)).toBeDefined();
  });

  it("labels a workflow-declared error code as metadata", () => {
    const block = buildBlock({
      block_type: "code",
      error_codes: ["inventory_unavailable"],
      failure_reason: "No inventory was available.",
      output: {
        errors: [
          {
            error_code: "inventory_unavailable",
            error_type: "USER_DEFINED_ERROR",
          },
        ],
      },
    });
    render(<BlockInspector block={block} />);

    expect(
      screen.getByText('The workflow reported "inventory_unavailable"'),
    ).toBeDefined();
    expect(screen.getByText("Error code inventory_unavailable")).toBeDefined();
  });

  it("omits technical details when a classified failure has no raw reason", () => {
    const block = buildBlock({
      block_type: "code",
      error_codes: ["runner_unavailable"],
      failure_reason: null,
    });
    render(<BlockInspector block={block} />);

    expect(screen.getByText("The code sandbox was unreachable")).toBeDefined();
    expect(screen.queryByText("Technical details")).toBeNull();
  });
});
