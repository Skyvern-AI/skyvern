// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type ReactNode } from "react";

import { Status } from "@/api/types";
import type {
  WorkflowRunBlock,
  WorkflowRunTimelineBlockItem as TimelineBlockItem,
  WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";
import type { WorkflowBlock } from "../types/workflowTypes";
import type { WorkflowRunOverviewActiveElement } from "./WorkflowRunOverview";
import { WorkflowRunTimeline } from "./WorkflowRunTimeline";

const mocks = vi.hoisted(() => ({
  workflowRun: undefined as unknown,
  timeline: undefined as unknown,
}));

vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => ({
    data: mocks.workflowRun,
    isLoading: false,
  }),
}));
vi.mock("../hooks/useWorkflowRunTimelineQuery", () => ({
  useWorkflowRunTimelineQuery: () => ({
    data: mocks.timeline,
    isLoading: false,
  }),
}));
// Radix ScrollArea needs ResizeObserver, which jsdom doesn't provide.
vi.mock("@/components/ui/scroll-area", () => ({
  ScrollArea: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  ScrollAreaViewport: ({ children }: { children?: ReactNode }) => (
    <div>{children}</div>
  ),
}));

function buildBlock(
  overrides: Partial<WorkflowRunBlock> = {},
): WorkflowRunBlock {
  return {
    workflow_run_block_id: "wrb_default",
    workflow_run_id: "wr_default",
    parent_workflow_run_block_id: null,
    block_type: "http_request",
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
    ...overrides,
  };
}

function buildBlockItem(
  block: WorkflowRunBlock,
  children: Array<WorkflowRunTimelineItem> = [],
): TimelineBlockItem {
  return {
    type: "block",
    block,
    children,
    thought: null,
    created_at: block.created_at,
    modified_at: block.modified_at,
  };
}

const noop = () => {};

function renderTimeline(activeItem: WorkflowRunOverviewActiveElement) {
  return render(
    <WorkflowRunTimeline
      activeItem={activeItem}
      onLiveStreamSelected={noop}
      onActionItemSelected={noop}
      onBlockItemSelected={noop}
      onThoughtItemSelected={noop}
      onIterationSelected={noop}
    />,
  );
}

function expectDomOrder(labels: Array<string>) {
  const nodes = labels.map((label) => screen.getByText(label));
  for (let i = 0; i < nodes.length - 1; i++) {
    expect(
      nodes[i]!.compareDocumentPosition(nodes[i + 1]!) &
        Node.DOCUMENT_POSITION_FOLLOWING,
      `expected "${labels[i]}" to render before "${labels[i + 1]}"`,
    ).toBeTruthy();
  }
}

afterEach(() => {
  cleanup();
  mocks.workflowRun = undefined;
  mocks.timeline = undefined;
});

describe("WorkflowRunTimeline", () => {
  it("renders blocks in global execution order, not branch-tree order", () => {
    // Regression: block_8/block_12 are branch children of conditional
    // block_2 but executed after root loop block_5. The tree rendering used
    // to print them above the loop, implying the run continued past the
    // terminated block.
    const conditional = buildBlock({
      workflow_run_block_id: "wrb_block_2",
      block_type: "conditional",
      label: "block_2",
      created_at: "2026-06-10T07:16:29Z",
    });
    const loop = buildBlock({
      workflow_run_block_id: "wrb_block_5",
      block_type: "for_loop",
      label: "block_5",
      loop_values: ["account_1"],
      created_at: "2026-06-10T07:19:06Z",
    });
    const loopChild = buildBlock({
      workflow_run_block_id: "wrb_goto_viewbill",
      block_type: "navigation",
      label: "goto_viewbill",
      parent_workflow_run_block_id: "wrb_block_5",
      created_at: "2026-06-10T07:19:11Z",
      current_index: 0,
    });
    const branchConditional = buildBlock({
      workflow_run_block_id: "wrb_block_8",
      block_type: "conditional",
      label: "block_8",
      parent_workflow_run_block_id: "wrb_block_2",
      created_at: "2026-06-10T07:29:32Z",
    });
    const terminated = buildBlock({
      workflow_run_block_id: "wrb_block_12",
      block_type: "navigation",
      label: "block_12",
      status: Status.Terminated,
      parent_workflow_run_block_id: "wrb_block_8",
      created_at: "2026-06-10T07:39:31Z",
    });

    mocks.workflowRun = {
      status: Status.Terminated,
      total_steps: 0,
      credits_used: 0,
      cached_credits_used: 0,
      workflow: {
        workflow_definition: { blocks: [], finally_block_label: null },
      },
    };
    mocks.timeline = [
      buildBlockItem(conditional, [
        buildBlockItem(branchConditional, [buildBlockItem(terminated)]),
      ]),
      buildBlockItem(loop, [buildBlockItem(loopChild)]),
    ];

    // Selecting the loop child keeps the loop and its iteration expanded so
    // the nested row is visible for the ordering assertion.
    renderTimeline(loopChild);

    expectDomOrder([
      "block_2",
      "block_5",
      "goto_viewbill",
      "block_8",
      "block_12",
    ]);
  });

  it("renders the terminated block as the last row", () => {
    const conditional = buildBlock({
      workflow_run_block_id: "wrb_cond",
      block_type: "conditional",
      label: "branch_check",
      created_at: "2026-06-10T07:16:29Z",
    });
    const loop = buildBlock({
      workflow_run_block_id: "wrb_loop",
      block_type: "for_loop",
      label: "download_loop",
      created_at: "2026-06-10T07:19:06Z",
    });
    const terminated = buildBlock({
      workflow_run_block_id: "wrb_terminated",
      block_type: "navigation",
      label: "final_navigation",
      status: Status.Terminated,
      parent_workflow_run_block_id: "wrb_cond",
      created_at: "2026-06-10T07:39:31Z",
    });

    mocks.workflowRun = {
      status: Status.Terminated,
      total_steps: 0,
      credits_used: 0,
      cached_credits_used: 0,
      workflow: {
        workflow_definition: { blocks: [], finally_block_label: null },
      },
    };
    mocks.timeline = [
      buildBlockItem(conditional, [buildBlockItem(terminated)]),
      buildBlockItem(loop),
    ];

    renderTimeline(null);

    expectDomOrder(["branch_check", "download_loop", "final_navigation"]);
  });

  it("does not show a 'did not execute' ghost for blocks that ran inside a branch", () => {
    const conditional = buildBlock({
      workflow_run_block_id: "wrb_cond",
      block_type: "conditional",
      label: "branch_check",
      created_at: "2026-06-10T07:16:29Z",
    });
    const branchChild = buildBlock({
      workflow_run_block_id: "wrb_branch_child",
      block_type: "navigation",
      label: "block_8",
      parent_workflow_run_block_id: "wrb_cond",
      created_at: "2026-06-10T07:29:32Z",
    });

    mocks.workflowRun = {
      status: Status.Completed,
      total_steps: 0,
      credits_used: 0,
      cached_credits_used: 0,
      workflow: {
        workflow_definition: {
          finally_block_label: null,
          blocks: [
            { block_type: "navigation", label: "block_8" },
            { block_type: "navigation", label: "never_ran" },
          ] as unknown as Array<WorkflowBlock>,
        },
      },
    };
    mocks.timeline = [
      buildBlockItem(conditional, [buildBlockItem(branchChild)]),
    ];

    renderTimeline(null);

    // block_8 executed (inside the branch) — exactly one row, no ghost.
    expect(screen.getAllByText("block_8")).toHaveLength(1);
    // never_ran is the only unexecuted defined block.
    expect(screen.getAllByText("did not execute")).toHaveLength(1);
    expect(screen.getByText("never_ran")).toBeDefined();
  });

  it("labels not-taken branch ghosts as skipped and unreached ones as did not execute", () => {
    const conditional = buildBlock({
      workflow_run_block_id: "wrb_cond",
      block_type: "conditional",
      label: "branch_check",
      created_at: "2026-06-10T07:16:29Z",
      output: {
        evaluations: [
          {
            branch_id: "br_taken",
            branch_index: 0,
            criteria_type: "jinja2_template",
            original_expression: "{{ found }}",
            rendered_expression: "true",
            result: true,
            is_matched: true,
            is_default: false,
            next_block_label: "block_8",
            error: null,
          },
          {
            branch_id: "br_other",
            branch_index: 1,
            criteria_type: "jinja2_template",
            original_expression: "{{ needs_other_path }}",
            rendered_expression: "false",
            result: false,
            is_matched: false,
            is_default: false,
            next_block_label: "other_path",
            error: null,
          },
        ],
      } as WorkflowRunBlock["output"],
    });
    const takenChild = buildBlock({
      workflow_run_block_id: "wrb_block_8",
      block_type: "navigation",
      label: "block_8",
      status: Status.Terminated,
      parent_workflow_run_block_id: "wrb_cond",
      created_at: "2026-06-10T07:29:32Z",
    });

    mocks.workflowRun = {
      status: Status.Terminated,
      total_steps: 0,
      credits_used: 0,
      cached_credits_used: 0,
      workflow: {
        workflow_definition: {
          finally_block_label: null,
          blocks: [
            {
              block_type: "conditional",
              label: "branch_check",
              branch_conditions: [
                {
                  id: "br_taken",
                  next_block_label: "block_8",
                  is_default: false,
                },
                {
                  id: "br_other",
                  description: "Use alternate path",
                  criteria: {
                    description: "Alternate path needed",
                  },
                  next_block_label: "other_path",
                  is_default: false,
                },
              ],
            },
            {
              block_type: "navigation",
              label: "block_8",
              next_block_label: "tail_block",
            },
            { block_type: "navigation", label: "other_path" },
            { block_type: "navigation", label: "tail_block" },
          ] as unknown as Array<WorkflowBlock>,
        },
      },
    };
    mocks.timeline = [
      buildBlockItem(conditional, [buildBlockItem(takenChild)]),
    ];

    const { container } = renderTimeline(null);

    const branchType = within(container).getByText("B • Else If");
    expect(branchType.className).toContain("text-muted-foreground");
    expect(branchType.className).not.toMatch(
      /\b(?:rounded|border(?:-\S+)?|bg-\S+|p[xy]-\S+)\b/,
    );
    expect(screen.getByText("· Use alternate path")).toBeDefined();
    expect(screen.getByText("condition false")).toBeDefined();
    expect(screen.getByText("1 block")).toBeDefined();
    expect(screen.queryByText("skipped")).toBeNull();
    const notReachedBadge = screen.getByText("did not execute");
    expect(notReachedBadge.closest("div.min-w-0")?.textContent).toContain(
      "tail_block",
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Expand skipped branch" }),
    );
    const skippedBadge = screen.getByText("skipped");
    expect(skippedBadge.closest("div.min-w-0")?.textContent).toContain(
      "other_path",
    );
    expectDomOrder([
      "branch_check",
      "B • Else If",
      "other_path",
      "block_8",
      "tail_block",
    ]);
  });
});
