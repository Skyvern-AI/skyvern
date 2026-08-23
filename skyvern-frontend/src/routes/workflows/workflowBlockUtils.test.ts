import { describe, expect, it } from "vitest";

import {
  buildCodeStepsByLabel,
  describeRecordedAction,
  findCodeStepForLine,
  getCodeStepPlainText,
  visitWorkflowBlocks,
} from "./workflowBlockUtils";
import type { ActionsApiResponse } from "@/api/types";
import type {
  CodeBlock,
  CodeBlockStep,
  ForLoopBlock,
  WorkflowBlock,
} from "./types/workflowTypes";

function codeBlock(
  label: string,
  steps: Array<CodeBlockStep> | null,
): CodeBlock {
  return {
    label,
    block_type: "code",
    output_parameter: { key: `${label}_output` },
    continue_on_failure: false,
    model: null,
    code: "",
    parameters: [],
    steps,
  } as unknown as CodeBlock;
}

function forLoop(
  label: string,
  loopBlocks: Array<WorkflowBlock>,
): ForLoopBlock {
  return {
    label,
    block_type: "for_loop",
    output_parameter: { key: `${label}_output` },
    continue_on_failure: false,
    model: null,
    loop_blocks: loopBlocks,
  } as unknown as ForLoopBlock;
}

describe("buildCodeStepsByLabel", () => {
  it("maps code block labels to their step outline", () => {
    const steps: Array<CodeBlockStep> = [
      { action_type: "goto", title: "Open page", line_start: 1, line_end: 1 },
    ];
    const map = buildCodeStepsByLabel([codeBlock("run_script", steps)]);
    expect(map.get("run_script")).toEqual(steps);
    expect(map.size).toBe(1);
  });

  it("descends into loop bodies", () => {
    const steps: Array<CodeBlockStep> = [
      { action_type: "click", description: "Click submit" },
    ];
    const map = buildCodeStepsByLabel([
      forLoop("loop", [codeBlock("nested_code", steps)]),
    ]);
    expect(map.get("nested_code")).toEqual(steps);
    expect(map.size).toBe(1);
  });

  it("skips code blocks with empty or null steps and non-code blocks", () => {
    const map = buildCodeStepsByLabel([
      codeBlock("empty", []),
      codeBlock("nullish", null),
      forLoop("loop", []),
    ]);
    expect(map.size).toBe(0);
  });
});

describe("visitWorkflowBlocks", () => {
  it("visits top-level and loop body blocks in display order", () => {
    const visited: Array<string> = [];

    visitWorkflowBlocks(
      [
        codeBlock("top", []),
        forLoop("loop", [codeBlock("nested", []), codeBlock("nested_2", [])]),
      ],
      (block) => {
        visited.push(block.label);
      },
    );

    expect(visited).toEqual(["top", "loop", "nested", "nested_2"]);
  });

  it("stops walking when the visitor returns false", () => {
    const visited: Array<string> = [];

    visitWorkflowBlocks(
      [
        codeBlock("top", []),
        forLoop("loop", [codeBlock("nested", []), codeBlock("nested_2", [])]),
      ],
      (block) => {
        visited.push(block.label);
        return block.label === "nested" ? false : undefined;
      },
    );

    expect(visited).toEqual(["top", "loop", "nested"]);
  });

  it("follows next_block_label chains so conditional branches precede their merge block", () => {
    // Editor-serialized order: the top-level chain (conditional → merge) is
    // emitted first and branch children are appended after it.
    const branch = (id: string, next_block_label: string) => ({
      id,
      criteria: null,
      next_block_label,
      description: null,
      is_default: false,
    });
    const blocks = [
      {
        ...codeBlock("check", []),
        block_type: "conditional",
        next_block_label: "end",
        branch_conditions: [branch("b1", "if_1"), branch("b2", "else_1")],
      },
      { ...codeBlock("end", []), next_block_label: null },
      { ...codeBlock("if_1", []), next_block_label: "loop" },
      {
        ...forLoop("loop", [codeBlock("inside", [])]),
        next_block_label: "if_2",
      },
      { ...codeBlock("if_2", []), next_block_label: "end" },
      { ...codeBlock("else_1", []), next_block_label: "end" },
    ] as unknown as Array<WorkflowBlock>;
    const visited: Array<string> = [];

    visitWorkflowBlocks(blocks, (block) => {
      visited.push(block.label);
    });

    expect(visited).toEqual([
      "check",
      "if_1",
      "loop",
      "inside",
      "if_2",
      "else_1",
      "end",
    ]);
  });
});

describe("getCodeStepPlainText", () => {
  it("prefers the step title", () => {
    expect(
      getCodeStepPlainText({
        action_type: "extract",
        title: "Extract the product details",
        description: "page.extract",
      }),
    ).toBe("Extract the product details");
  });

  it("falls back to the description when there is no title", () => {
    expect(
      getCodeStepPlainText({
        action_type: "click",
        description: "Click submit",
      }),
    ).toBe("Click submit");
  });

  it("humanizes the action type when title and description are absent", () => {
    expect(getCodeStepPlainText({ action_type: "extract" })).toBe(
      "Extract Data",
    );
    expect(getCodeStepPlainText({ action_type: "go_forward" })).toBe(
      "Go Forward",
    );
  });

  it("ignores blank title and description", () => {
    expect(
      getCodeStepPlainText({
        action_type: "extract",
        title: "   ",
        description: "",
      }),
    ).toBe("Extract Data");
  });
});

describe("findCodeStepForLine", () => {
  const steps: Array<CodeBlockStep> = [
    { action_type: "goto", title: "Open page", line_start: 1, line_end: 1 },
    { action_type: "click", title: "Submit", line_start: 3, line_end: 6 },
    { action_type: "extract", title: "No line position" },
  ];

  it("returns null when the action carries no code line", () => {
    expect(findCodeStepForLine(steps, null)).toBeNull();
  });

  it("matches a step by exact line_start", () => {
    expect(findCodeStepForLine(steps, 1)?.title).toBe("Open page");
  });

  it("matches a step by range containment when no exact line_start matches", () => {
    expect(findCodeStepForLine(steps, 4)?.title).toBe("Submit");
  });

  it("prefers an exact line_start over a containing range", () => {
    const overlapping: Array<CodeBlockStep> = [
      { action_type: "click", title: "Range", line_start: 1, line_end: 5 },
      { action_type: "extract", title: "Exact", line_start: 3, line_end: 3 },
    ];
    expect(findCodeStepForLine(overlapping, 3)?.title).toBe("Exact");
  });

  it("returns null when no step covers the line", () => {
    expect(findCodeStepForLine(steps, 99)).toBeNull();
  });
});

describe("describeRecordedAction", () => {
  // Shaped like the timeline wire payload: WorkflowRunBlock.actions serializes as list[Action],
  // so only base-Action fields exist here. url/keys are subclass-only and never arrive.
  function action(
    overrides: Partial<ActionsApiResponse> = {},
  ): ActionsApiResponse {
    return {
      action_id: "act_1",
      action_type: "click",
      status: "completed",
      task_id: null,
      step_id: null,
      step_order: null,
      action_order: 0,
      confidence_float: null,
      description: "locator.click div:nth-of-type(1) > button:nth-of-type(1)",
      reasoning: null,
      intention: null,
      response: null,
      created_by: null,
      text: null,
      ...overrides,
    } as ActionsApiResponse;
  }

  it("names the action from the definition step it fired from", () => {
    const step: CodeBlockStep = {
      action_type: "click",
      description: "Click the Sign in button",
      line_start: 4,
    };
    expect(describeRecordedAction(action(), step)).toBe(
      "Click the Sign in button",
    );
  });

  it("ignores a step whose kind disagrees, so a drifted outline cannot mislabel", () => {
    const drifted: CodeBlockStep = {
      action_type: "input_text",
      description: "Type into the username field",
      line_start: 4,
    };
    expect(describeRecordedAction(action(), drifted)).toBeNull();
  });

  it("returns null rather than the type name, so a caller showing the label is not doubled", () => {
    expect(describeRecordedAction(action(), null)).toBeNull();
    expect(
      describeRecordedAction(
        action({ action_type: "input_text", text: "" }),
        null,
      ),
    ).toBeNull();
  });

  it("never renders the raw selector, with or without a step", () => {
    for (const step of [null, { action_type: "extract" } as CodeBlockStep]) {
      const text = describeRecordedAction(action(), step) ?? "";
      expect(text).not.toContain("locator.");
      expect(text).not.toContain("nth-of-type");
    }
  });

  it("falls back to the prose a failed recorded action carries in response", () => {
    // The recorder only ever populates response, and only in its except branch.
    expect(
      describeRecordedAction(
        action({ response: "TimeoutError: locator not visible" }),
        null,
      ),
    ).toBe("TimeoutError: locator not visible");
  });

  it("keeps the author's own prompt, which the recorder stores in description", () => {
    expect(
      describeRecordedAction(
        action({
          action_type: "extract",
          description: "Read the order confirmation number",
        }),
        null,
      ),
    ).toBe("Read the order confirmation number");
  });

  it("reads a navigation target out of the recorder trace, the only argument worth showing", () => {
    expect(
      describeRecordedAction(
        action({
          action_type: "goto_url",
          description: "page.goto https://example.com/login",
        }),
        null,
      ),
    ).toBe("Open https://example.com/login");
  });

  it("names a download from file_name, which the base Action does carry", () => {
    expect(
      describeRecordedAction(
        action({ action_type: "download_file", file_name: "invoice.pdf" }),
        null,
      ),
    ).toBe("Download invoice.pdf");
  });
});
