import { describe, expect, test } from "vitest";

import type { WorkflowBlock } from "../../types/workflowTypes";
import { collectBlockPrompts } from "./blockPrompts";

function block(value: Record<string, unknown>): WorkflowBlock {
  return {
    output_parameter: { key: "synthetic_output" },
    continue_on_failure: false,
    model: null,
    ...value,
  } as unknown as WorkflowBlock;
}

describe("collectBlockPrompts", () => {
  test("collects all six fields in priority order and keeps multiple fields per block", () => {
    const prompts = collectBlockPrompts([
      block({
        block_type: "task",
        label: "multi-field block",
        navigation_goal: "Navigate to the next step",
        data_extraction_goal: "Extract the summary",
        complete_criterion: "Stop after success",
        terminate_criterion: "Stop after failure",
      }),
      block({
        block_type: "text_prompt",
        label: "prompt block",
        prompt: "Summarize the supplied text",
      }),
      block({
        block_type: "human_interaction",
        label: "instruction block",
        instructions: "Request a confirmation",
      }),
    ]);

    expect(prompts).toEqual([
      {
        blockLabel: "multi-field block",
        blockType: "task",
        fields: [
          {
            fieldLabel: "Navigation goal",
            prompt: "Navigate to the next step",
          },
          {
            fieldLabel: "Extraction goal",
            prompt: "Extract the summary",
          },
          {
            fieldLabel: "Completion criterion",
            prompt: "Stop after success",
          },
          {
            fieldLabel: "Termination criterion",
            prompt: "Stop after failure",
          },
        ],
      },
      {
        blockLabel: "prompt block",
        blockType: "text_prompt",
        fields: [
          { fieldLabel: "Prompt", prompt: "Summarize the supplied text" },
        ],
      },
      {
        blockLabel: "instruction block",
        blockType: "human_interaction",
        fields: [
          { fieldLabel: "Instructions", prompt: "Request a confirmation" },
        ],
      },
    ]);
  });

  test("emits a block with only a termination criterion", () => {
    expect(
      collectBlockPrompts([
        block({
          block_type: "validation",
          label: "termination-only block",
          complete_criterion: null,
          terminate_criterion: "Terminate when the value is invalid",
        }),
      ]),
    ).toEqual([
      {
        blockLabel: "termination-only block",
        blockType: "validation",
        fields: [
          {
            fieldLabel: "Termination criterion",
            prompt: "Terminate when the value is invalid",
          },
        ],
      },
    ]);
  });

  test("flattens nested for and while loops in pre-order", () => {
    const prompts = collectBlockPrompts([
      block({
        block_type: "task_v2",
        label: "first block",
        prompt: "First prompt",
      }),
      block({
        block_type: "for_loop",
        label: "outer loop",
        loop_blocks: [
          block({
            block_type: "text_prompt",
            label: "for child",
            prompt: "For-loop prompt",
          }),
          block({
            block_type: "while_loop",
            label: "inner loop",
            condition: {
              criteria_type: "prompt",
              expression: "Deferred condition prompt",
              description: null,
            },
            loop_blocks: [
              block({
                block_type: "human_interaction",
                label: "while child",
                instructions: "While-loop instructions",
              }),
            ],
          }),
        ],
      }),
    ]);

    expect(prompts.map(({ blockLabel }) => blockLabel)).toEqual([
      "first block",
      "for child",
      "while child",
    ]);
  });

  test("preserves non-empty whitespace, newlines, and templates verbatim", () => {
    const prompt = "  First line\n    {{ synthetic_parameter }}\n  Last line  ";

    expect(
      collectBlockPrompts([
        block({ block_type: "code", label: "template block", prompt }),
      ]),
    ).toEqual([
      {
        blockLabel: "template block",
        blockType: "code",
        fields: [{ fieldLabel: "Prompt", prompt }],
      },
    ]);
  });

  test("omits null, empty, whitespace-only, and non-prompt fields", () => {
    expect(
      collectBlockPrompts([
        block({
          block_type: "task",
          label: "empty prompt fields",
          navigation_goal: null,
          data_extraction_goal: "",
          complete_criterion: "   ",
          terminate_criterion: null,
        }),
        block({
          block_type: "goto_url",
          label: "url block",
          url: "/synthetic",
        }),
        block({ block_type: "wait", label: "wait block", wait_sec: 1 }),
        block({
          block_type: "http_request",
          label: "request block",
          url: "/synthetic",
        }),
      ]),
    ).toEqual([]);
  });

  test("does not mutate the input definition", () => {
    const blocks = [
      block({
        block_type: "for_loop",
        label: "loop block",
        loop_blocks: [
          block({
            block_type: "task_v2",
            label: "nested block",
            prompt: "Nested prompt",
          }),
        ],
      }),
    ];
    const before = JSON.stringify(blocks);

    collectBlockPrompts(blocks);

    expect(JSON.stringify(blocks)).toBe(before);
  });
});
