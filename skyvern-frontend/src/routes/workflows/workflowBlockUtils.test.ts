import { describe, expect, it } from "vitest";

import { buildCodeStepsByLabel } from "./workflowBlockUtils";
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
