import { describe, expect, it } from "vitest";

import {
  buildCodeStepsByLabel,
  findCodeStepForLine,
  getCodeStepPlainText,
  visitWorkflowBlocks,
} from "./workflowBlockUtils";
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
