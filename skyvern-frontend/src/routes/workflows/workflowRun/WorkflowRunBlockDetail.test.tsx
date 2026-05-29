// @vitest-environment jsdom

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => ({ data: null }),
}));
vi.mock("../hooks/useWorkflowRunTimelineQuery", () => ({
  useWorkflowRunTimelineQuery: () => ({ data: [], isLoading: false }),
}));
vi.mock("./WorkflowRunHumanInteraction", () => ({
  WorkflowRunHumanInteraction: () => null,
}));

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ActionTypes, Status, type ActionsApiResponse } from "@/api/types";
import type {
  WorkflowRunBlock,
  WorkflowRunTimelineBlockItem,
} from "../types/workflowRunTypes";
import { WorkflowRunBlockDetail } from "./WorkflowRunBlockDetail";

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

function buildBlockItem(block: WorkflowRunBlock): WorkflowRunTimelineBlockItem {
  return {
    type: "block",
    block,
    children: [],
    thought: null,
    created_at: block.created_at,
    modified_at: block.modified_at,
  };
}

afterEach(() => {
  cleanup();
});

describe("WorkflowRunBlockDetail router", () => {
  it("renders the extraction detail header for an extraction block but no longer shows the goal/criteria fields", () => {
    const block = buildBlock({
      block_type: "extraction",
      data_extraction_goal: "Pull the price from the page",
    });
    render(<WorkflowRunBlockDetail activeItem={block} timeline={[]} />);
    // Header still labels the block type
    expect(screen.getByText("Extraction")).toBeDefined();
    // Goal field is intentionally hidden in the detail body (feedback #4/#6/#9)
    expect(screen.queryByText(/extraction goal/i)).toBeNull();
    expect(screen.queryByText("Pull the price from the page")).toBeNull();
  });

  it("renders the failure_reason in the detail body (not the header) when set", () => {
    const block = buildBlock({
      block_type: "extraction",
      failure_reason: "Could not locate the price element",
    });
    render(<WorkflowRunBlockDetail activeItem={block} timeline={[]} />);
    expect(screen.getByText("Failure")).toBeDefined();
    expect(
      screen.getAllByText("Could not locate the price element").length,
    ).toBeGreaterThanOrEqual(1);
  });

  it("renders block inputs and searchable outputs inside the detail panel inspector", () => {
    const block = buildBlock({
      block_type: "http_request",
      label: "fetch_report",
      url: "https://example.test/report",
      output: {
        status_code: 200,
        response_json: {
          result: "low_value",
          nested: { keep: "visible" },
        },
      },
    });

    render(<WorkflowRunBlockDetail activeItem={block} timeline={[]} />);

    expect(screen.getByText("output")).toBeDefined();
    expect(screen.getAllByText("status_code").length).toBeGreaterThanOrEqual(1);
    expect(
      screen.getAllByPlaceholderText("Search JSON").length,
    ).toBeGreaterThanOrEqual(1);

    fireEvent.change(screen.getAllByPlaceholderText("Search JSON")[0]!, {
      target: { value: "low_value" },
    });
    expect(
      screen.getAllByText(
        (_, element) => element?.textContent === '"low_value"',
      ).length,
    ).toBeGreaterThanOrEqual(1);

    fireEvent.click(screen.getByRole("tab", { name: "Inputs" }));
    expect(screen.getByText("URL")).toBeDefined();
    expect(screen.getByText("https://example.test/report")).toBeDefined();
  });

  it("renders the conditional detail (branch evaluation) for a conditional block", () => {
    const block = buildBlock({
      block_type: "conditional",
      executed_branch_id: "b1",
      output: {
        evaluations: [
          {
            branch_id: "b1",
            branch_index: 0,
            criteria_type: "jinja2_template",
            original_expression: "{{ x == 1 }}",
            rendered_expression: "1 == 1",
            result: true,
            is_matched: true,
            is_default: false,
            next_block_label: "next_block",
            error: null,
          },
        ],
      },
    });
    render(<WorkflowRunBlockDetail activeItem={block} timeline={[]} />);
    // The branch evaluations section renders the expression as a code chunk
    expect(screen.getByText("{{ x == 1 }}")).toBeDefined();
  });

  it("renders the loop detail (iterable values) for a for_loop block with values", () => {
    const block = buildBlock({
      block_type: "for_loop",
      loop_values: [{ name: "alpha" }, { name: "beta" }],
    });
    render(<WorkflowRunBlockDetail activeItem={block} timeline={[]} />);
    // Header section enumerates the iterable count
    expect(screen.getByText(/iterable values \(2\)/i)).toBeDefined();
  });

  it("renders the http_request detail (URL) for an http_request block", () => {
    const block = buildBlock({
      block_type: "http_request",
      url: "https://example.test/endpoint",
    });
    render(<WorkflowRunBlockDetail activeItem={block} timeline={[]} />);
    expect(screen.getByText("https://example.test/endpoint")).toBeDefined();
  });

  it("renders an empty state when there is no selection and no resolvable target", () => {
    render(<WorkflowRunBlockDetail activeItem={null} timeline={[]} />);
    expect(screen.getByText(/no block selected/i)).toBeDefined();
  });

  it("renders the human_interaction detail (instructions + recipients) for a human_interaction block", () => {
    const block = buildBlock({
      block_type: "human_interaction",
      instructions: "Please verify the order total before we ship.",
      subject: "SKY-10066 approval",
      recipients: ["celal@skyvern.com", "ops@skyvern.com"],
    });
    render(<WorkflowRunBlockDetail activeItem={block} timeline={[]} />);
    expect(
      screen.getByText("Please verify the order total before we ship."),
    ).toBeDefined();
    expect(screen.getByText("SKY-10066 approval")).toBeDefined();
    expect(screen.getByText("celal@skyvern.com")).toBeDefined();
    expect(screen.getByText("ops@skyvern.com")).toBeDefined();
  });

  it("defaults to the running block when no selection but the run has one", () => {
    const running = buildBlock({
      workflow_run_block_id: "wrb_running",
      block_type: "extraction",
      label: "extract_posts",
      data_extraction_goal: "Pull the running goal",
      status: Status.Running,
    });
    render(
      <WorkflowRunBlockDetail
        activeItem="stream"
        timeline={[buildBlockItem(running)]}
      />,
    );
    // Should render the running extraction's detail header
    expect(screen.getByText("Extraction")).toBeDefined();
    expect(screen.getByText("extract_posts")).toBeDefined();
    expect(screen.getByText("Running")).toBeDefined();
  });

  it("surfaces the selected action directly under the block header", () => {
    const action: ActionsApiResponse = {
      action_id: "act_extract",
      action_type: ActionTypes.extract,
      status: Status.Completed,
      task_id: null,
      step_id: null,
      step_order: null,
      action_order: null,
      reasoning: "Extract the event date from the page",
      description: null,
      intention: null,
      response: null,
      text: null,
      created_by: null,
      confidence_float: 1,
    };
    const block = buildBlock({
      workflow_run_block_id: "wrb_task",
      block_type: "task_v2",
      label: "calendar_lookup",
      actions: [action],
    });

    render(
      <WorkflowRunBlockDetail
        activeItem={action}
        timeline={[buildBlockItem(block)]}
      />,
    );

    expect(screen.getByText("calendar_lookup")).toBeDefined();
    expect(screen.getByText("Selected action")).toBeDefined();
    expect(
      screen.getAllByText("Extract the event date from the page")[0],
    ).toBeDefined();
  });

  it("ignores a stale activeIteration when falling back via 'stream'", () => {
    // A prior selection set ?iteration=2; the user then jumped to Live so
    // activeItem became 'stream' and the resolved block is a loop unrelated
    // to that prior selection. The header must not show "Iteration 3".
    const loop = buildBlock({
      workflow_run_block_id: "wrb_loop_running",
      block_type: "for_loop",
      label: "iterate_items",
      loop_values: ["alpha", "beta", "gamma"],
      current_index: 0,
      current_value: "alpha",
      status: Status.Running,
    });
    render(
      <WorkflowRunBlockDetail
        activeItem="stream"
        activeIteration={2}
        timeline={[buildBlockItem(loop)]}
      />,
    );
    // Loop's own current_index=0 means the chip should say "Iteration 1",
    // not "Iteration 3" from the stale URL hint.
    expect(screen.queryByText(/Iteration 3/)).toBeNull();
    expect(screen.getByText(/Iteration 1/)).toBeDefined();
  });
});

describe("WorkflowRunBlockDetail cold-start skeleton", () => {
  it("renders the header skeleton and an empty body while the timeline query is loading", async () => {
    vi.resetModules();
    vi.doMock("../hooks/useWorkflowRunWithWorkflowQuery", () => ({
      useWorkflowRunWithWorkflowQuery: () => ({ data: null }),
    }));
    vi.doMock("../hooks/useWorkflowRunTimelineQuery", () => ({
      useWorkflowRunTimelineQuery: () => ({
        data: undefined,
        isLoading: false,
      }),
    }));
    vi.doMock("./WorkflowRunHumanInteraction", () => ({
      WorkflowRunHumanInteraction: () => null,
    }));
    const { WorkflowRunBlockDetail: ReloadedBlockDetail } =
      await import("./WorkflowRunBlockDetail");
    const { container } = render(
      <ReloadedBlockDetail
        activeItem={null}
        timeline={[]}
        timelineReady={false}
      />,
    );
    // No real block detail strings (block-type label, status, etc.) should
    // appear yet — only skeleton elements.
    expect(screen.queryByText(/no block selected/i)).toBeNull();
    expect(screen.queryByText(/iteration/i)).toBeNull();
    // The skeleton uses the shared Skeleton component which carries a
    // `bg-slate` style; pick out at least one such placeholder block.
    const skeletons = container.querySelectorAll('[class*="animate-pulse"]');
    expect(skeletons.length).toBeGreaterThan(0);
    vi.doUnmock("../hooks/useWorkflowRunTimelineQuery");
  });
});
