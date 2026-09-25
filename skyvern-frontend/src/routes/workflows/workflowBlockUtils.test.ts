import { describe, expect, it } from "vitest";

import {
  buildCodeStepsByLabel,
  describeRecordedAction,
  findCodeStepForLine,
  getActionInputValue,
  getActionOutcome,
  getActionSummary,
  getCodeStepPlainText,
  taskV3CallText,
  visitWorkflowBlocks,
} from "./workflowBlockUtils";
import { ActionTypes, Status, type ActionsApiResponse } from "@/api/types";
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
      {
        action_type: "goto",
        description: "Open page",
        line_start: 1,
        line_end: 1,
      },
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
  it("uses the description", () => {
    expect(
      getCodeStepPlainText({
        action_type: "click",
        description: "Click submit",
      }),
    ).toBe("Click submit");
  });

  it("humanizes the action type when the description is absent", () => {
    expect(getCodeStepPlainText({ action_type: "extract" })).toBe(
      "Extract Data",
    );
    expect(getCodeStepPlainText({ action_type: "go_forward" })).toBe(
      "Go Forward",
    );
  });

  it("ignores a blank description", () => {
    expect(
      getCodeStepPlainText({
        action_type: "extract",
        description: "   ",
      }),
    ).toBe("Extract Data");
  });
});

describe("findCodeStepForLine", () => {
  const steps: Array<CodeBlockStep> = [
    {
      action_type: "goto",
      description: "Open page",
      line_start: 1,
      line_end: 1,
    },
    { action_type: "click", description: "Submit", line_start: 3, line_end: 6 },
    { action_type: "extract", description: "No line position" },
  ];

  it("returns null when the action carries no code line", () => {
    expect(findCodeStepForLine(steps, null)).toBeNull();
  });

  it("matches a step by exact line_start", () => {
    expect(findCodeStepForLine(steps, 1)?.description).toBe("Open page");
  });

  it("matches a step by range containment when no exact line_start matches", () => {
    expect(findCodeStepForLine(steps, 4)?.description).toBe("Submit");
  });

  it("prefers an exact line_start over a containing range", () => {
    const overlapping: Array<CodeBlockStep> = [
      {
        action_type: "click",
        description: "Range",
        line_start: 1,
        line_end: 5,
      },
      {
        action_type: "extract",
        description: "Exact",
        line_start: 3,
        line_end: 3,
      },
    ];
    expect(findCodeStepForLine(overlapping, 3)?.description).toBe("Exact");
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

describe("taskV3CallText", () => {
  it("returns the tool call a Task V3 action row was stamped with", () => {
    expect(taskV3CallText("task_v3 click #sign-in")).toBe("click #sign-in");
  });

  it("keeps a selector that contains spaces whole", () => {
    expect(taskV3CallText('task_v3 click [data-tv3="7"] button')).toBe(
      'click [data-tv3="7"] button',
    );
  });

  it("returns the bare tool name when the call carried no argument", () => {
    expect(taskV3CallText("task_v3 scroll")).toBe("scroll");
  });

  it("ignores descriptions that are not Task V3 tool calls", () => {
    expect(taskV3CallText("locator.click #sign-in")).toBeNull();
    expect(taskV3CallText("Click the sign-in button")).toBeNull();
    expect(taskV3CallText(null)).toBeNull();
  });
});

describe("getActionInputValue", () => {
  // The recorder stores "" in text and the exception in response on a failed fill. Reading response
  // as the input there printed the error on the Input line and, because the inspector suppresses an
  // outcome that equals the input, dropped it from Outputs entirely.
  it("does not read a failed fill's exception as the value it typed", () => {
    const failedFill = {
      action_type: ActionTypes.InputText,
      status: Status.Failed,
      text: "",
      response: "Timeout 30000ms exceeded waiting for locator('#zip')",
    };

    expect(getActionInputValue(failedFill)).toBeNull();
    expect(getActionOutcome(failedFill)).toBe(
      "Timeout 30000ms exceeded waiting for locator('#zip')",
    );
  });

  it("reads a script-generated fill's value out of response", () => {
    expect(
      getActionInputValue({
        action_type: ActionTypes.InputText,
        status: Status.Completed,
        text: "",
        response: "Meridian Ave",
      }),
    ).toBe("Meridian Ave");
  });
});

describe("getActionSummary", () => {
  it("falls back to the intention when the model left no reasoning", () => {
    expect(
      getActionSummary({
        action_type: ActionTypes.GotoUrl,
        reasoning: null,
        intention: "Navigated to https://example.com/get-in-touch/",
        response: null,
        text: null,
      }),
    ).toEqual({
      body: {
        text: "Navigated to https://example.com/get-in-touch/",
        isProse: true,
      },
      outcome: null,
    });
  });

  // The dead-end navigation is why the outcome is not a fallback: the intention says where the
  // agent meant to go, and only the response says the page was a 404.
  it("keeps the recorded outcome beside an intention instead of behind it", () => {
    expect(
      getActionSummary({
        action_type: ActionTypes.GotoUrl,
        reasoning: null,
        intention: "Tried to navigate to https://example.com/contact-us/",
        response: "https://example.com/contact-us/ (HTTP 404, dead end)",
        text: null,
      }),
    ).toEqual({
      body: {
        text: "Tried to navigate to https://example.com/contact-us/",
        isProse: true,
      },
      outcome: "https://example.com/contact-us/ (HTTP 404, dead end)",
    });
  });

  it("keeps the recorded outcome beside the model's own reasoning", () => {
    expect(
      getActionSummary({
        action_type: ActionTypes.GotoUrl,
        reasoning: "**Following the footer link** to the contact page",
        intention: "Tried to navigate to https://example.com/contact-us/",
        response: "https://example.com/contact-us/ (HTTP 404, dead end)",
      }),
    ).toEqual({
      body: {
        text: "**Following the footer link** to the contact page",
        isProse: true,
      },
      outcome: "https://example.com/contact-us/ (HTTP 404, dead end)",
    });
  });

  // goto_url is the case that made this chain necessary: url is subclass-only and never reaches
  // the client, so a navigation row with no intention holds its destination only in response.
  it("carries the response alone, as literal text, when that is all a row has", () => {
    expect(
      getActionSummary({
        action_type: ActionTypes.GotoUrl,
        reasoning: "   ",
        intention: null,
        response: "https://example.com/contact-us/ (HTTP 404, dead end)",
      }),
    ).toEqual({
      body: null,
      outcome: "https://example.com/contact-us/ (HTTP 404, dead end)",
    });
  });

  // A cached run writes the answer it typed to both text and response, and the card already prints
  // it on its Input line.
  it("does not report a typed value back as an outcome", () => {
    expect(
      getActionSummary({
        action_type: ActionTypes.InputText,
        reasoning: null,
        intention: "Enter your zip code",
        response: "90210",
        text: "90210",
      }),
    ).toEqual({
      body: { text: "Enter your zip code", isProse: true },
      outcome: null,
    });
  });

  // Paragraph breaks survive for a card that renders blocks; collapsing them here would flatten a
  // multi-paragraph provider summary into one run-on line.
  it("keeps the prose body as written", () => {
    expect(
      getActionSummary({
        reasoning: "**Investigating the iframe**\n\nThen reading the table",
      })?.body,
    ).toEqual({
      text: "**Investigating the iframe**\n\nThen reading the table",
      isProse: true,
    });
  });

  it("returns null when a row carries no text at all, so the type pill is not doubled", () => {
    expect(getActionSummary({ reasoning: "", text: null })).toBeNull();
  });
});
