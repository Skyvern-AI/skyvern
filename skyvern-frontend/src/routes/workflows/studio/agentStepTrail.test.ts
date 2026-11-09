import { describe, expect, test } from "vitest";

import { Status } from "@/api/types";
import {
  type AgentStepToolCall,
  type WorkflowRunBlock,
  type WorkflowRunTimelineBlockItem,
  type WorkflowRunTimelineItem,
} from "@/routes/workflows/types/workflowRunTypes";

import {
  agentStepCallActions,
  agentStepCallFrameId,
  agentStepTrail,
  findAgentStepItem,
  toolCallArgumentFields,
} from "./agentStepTrail";

function blockItem(
  block: Partial<WorkflowRunBlock>,
  children: WorkflowRunTimelineItem[] = [],
): WorkflowRunTimelineBlockItem {
  return {
    type: "block",
    thought: null,
    children,
    created_at: "2026-01-01T00:00:00Z",
    modified_at: "2026-01-01T00:00:00Z",
    block: {
      workflow_run_block_id: `wrb_${block.label ?? "row"}`,
      workflow_run_id: "wr_1",
      parent_workflow_run_block_id: null,
      block_type: "task",
      label: null,
      description: null,
      title: null,
      status: Status.Completed,
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
      ...block,
    },
  };
}

function call(
  index: number,
  overrides: Partial<AgentStepToolCall> = {},
): AgentStepToolCall {
  return {
    index,
    tool: "page_goto",
    status: "completed",
    started_at: `2026-01-01T00:00:0${index}+00:00`,
    finished_at: `2026-01-01T00:00:0${index}.900000+00:00`,
    arguments: '{"url": "https://example.com/"}',
    ...overrides,
  };
}

describe("agentStepTrail", () => {
  test.each([undefined, null, { truncated: true }])(
    "a step without a call list (%s) draws no cards, even with child rows",
    (toolCalls) => {
      const child = blockItem({ label: "agent_task_1" });
      const step = blockItem(
        {
          block_type: "agent_step",
          label: "agent",
          tool_calls: toolCalls as WorkflowRunBlock["tool_calls"],
        },
        [child],
      );

      expect(agentStepTrail(step.block, step.children)).toEqual([]);
    },
  );

  test("orders calls by index and matches children by label", () => {
    const task = blockItem({ label: "agent_task_2", status: Status.Running });
    const stray = blockItem({ label: "older_child" });
    const step = blockItem(
      {
        block_type: "agent_step",
        label: "agent",
        status: Status.Running,
        tool_calls: [
          call(2, {
            tool: "browser_task",
            block_type: "task",
            label: "agent_task_2",
            status: "running",
            finished_at: null,
          }),
          call(1),
        ],
      },
      [stray, task],
    );

    const entries = agentStepTrail(step.block, step.children);

    expect(
      entries.map((entry) => [entry.call.index, entry.child, entry.running]),
    ).toEqual([
      [1, null, false],
      [2, task, true],
    ]);
  });

  test.each([Status.Canceled, Status.TimedOut])(
    "a call left running on a %s step does not spin and shows the step's status",
    (stepStatus) => {
      const step = blockItem({
        block_type: "agent_step",
        status: stepStatus,
        tool_calls: [
          call(1, { status: "invalid_arguments" }),
          call(2, { status: "skipped" }),
          call(3, { status: "running", finished_at: null }),
        ],
      });

      expect(
        agentStepTrail(step.block, []).map((entry) => [
          entry.running,
          entry.status,
        ]),
      ).toEqual([
        [false, Status.Failed],
        [false, Status.Skipped],
        [false, stepStatus],
      ]);
    },
  );
});

test("the newest agent step row with the label is the one shown", () => {
  const older = blockItem({
    workflow_run_block_id: "wrb_agent_1",
    block_type: "agent_step",
    label: "agent",
    created_at: "2026-01-01T00:00:00",
  });
  const newer = blockItem({
    block_type: "agent_step",
    label: "agent",
    created_at: "2026-01-01T00:01:00",
  });
  const loop = blockItem({ block_type: "for_loop" }, [older, newer]);

  expect(findAgentStepItem([loop], { label: "agent" })).toBe(newer);
  expect(findAgentStepItem([loop], { label: "other" })).toBeNull();
  expect(
    findAgentStepItem([loop], {
      workflowRunBlockId: "wrb_agent_1",
    }),
  ).toBe(older);
});

test("arguments become labelled fields, and text that is not JSON stays whole", () => {
  expect(
    toolCallArgumentFields(
      call(1, {
        arguments: '{"prompt": "price", "schema": {"type": "number"}}',
      }),
    ),
  ).toEqual([
    ["prompt", "price"],
    ["schema", '{\n  "type": "number"\n}'],
  ]);
  expect(toolCallArgumentFields(call(1, { arguments: '"{no"' }))).toEqual([
    ["arguments", "{no"],
  ]);
});

describe("agentStepCallFrameId", () => {
  const action = (id: string, created_at: string) => ({
    action_id: id,
    action_type: "click" as const,
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
    created_at,
  });
  const step = blockItem({
    block_type: "agent_step",
    label: "agent",
    actions: [
      action("act_before", "2026-01-01T00:00:00.500000"),
      action("act_during", "2026-01-01T00:00:01.500000"),
      action("act_newest", "2026-01-01T00:00:01.800000"),
    ],
  });

  test("a block call opens its child row", () => {
    const child = blockItem({ label: "agent_task_2" });
    expect(
      agentStepCallFrameId(step.block, {
        call: call(2),
        child,
        running: false,
        status: Status.Completed,
      }),
    ).toBe(child.block.workflow_run_block_id);
  });

  test("a page call opens the step's newest action inside the call, else the step", () => {
    expect(
      agentStepCallFrameId(step.block, {
        call: call(1, { finished_at: "2026-01-01T00:00:02+00:00" }),
        child: null,
        running: false,
        status: Status.Completed,
      }),
    ).toBe("act_newest");
    expect(
      agentStepCallFrameId(step.block, {
        call: call(5),
        child: null,
        running: false,
        status: Status.Completed,
      }),
    ).toBe(step.block.workflow_run_block_id);
  });

  test("an action on the edge two calls share belongs only to the later call", () => {
    const shared = blockItem({
      block_type: "agent_step",
      actions: [
        action("act_stray", "2026-01-01T00:00:00.500000"),
        action("act_edge", "2026-01-01T00:00:02"),
      ],
      tool_calls: [
        call(1, { finished_at: "2026-01-01T00:00:02+00:00" }),
        call(2),
      ],
    });
    const [first, second] = shared.block.tool_calls ?? [];

    expect(agentStepCallActions(shared.block, first!)).toEqual([]);
    expect(
      agentStepCallActions(shared.block, second!).map((a) => a.action_id),
    ).toEqual(["act_edge"]);
  });
});
