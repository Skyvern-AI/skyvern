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
vi.mock("../hooks/useRunHealEpisodesQuery", () => ({
  useRunHealEpisodesQuery: () => ({
    data: {
      episodes: [],
      summary: {
        blocks_healed: 0,
        blocks_outcome_risk: [],
        blocks_with_heal_attempt: 0,
      },
    },
  }),
}));
vi.mock("./WorkflowRunHumanInteraction", () => ({
  WorkflowRunHumanInteraction: () => null,
}));

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
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

function buildAction(
  overrides: Partial<ActionsApiResponse> = {},
): ActionsApiResponse {
  return {
    action_id: "act_default",
    action_type: ActionTypes.NullAction,
    status: Status.Completed,
    task_id: "tsk_code",
    step_id: null,
    step_order: 0,
    action_order: 0,
    reasoning: "Recorded code step",
    description: null,
    intention: null,
    response: null,
    text: null,
    created_by: null,
    confidence_float: null,
    ...overrides,
  };
}

function renderActionDiagnosticsHref(
  activeAction: ActionsApiResponse,
  actions: Array<ActionsApiResponse>,
): string | null {
  const block = buildBlock({
    block_type: "code",
    task_id: "tsk_code",
    actions,
  });

  render(
    <MemoryRouter>
      <WorkflowRunBlockDetail
        activeItem={activeAction}
        timeline={[buildBlockItem(block)]}
      />
    </MemoryRouter>,
  );

  return screen
    .getByRole("link", {
      name: /diagnostics/i,
    })
    .getAttribute("href");
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

  it("renders code block extracted information without the raw output wrapper", () => {
    const block = buildBlock({
      block_type: "code",
      label: "collect_data",
      output: {
        extracted_information: {
          account_status: "active",
          reference_id: "ref_123",
        },
        raw_code_output: "debug payload",
      },
    });

    render(<WorkflowRunBlockDetail activeItem={block} timeline={[]} />);

    expect(
      screen.getByRole("tab", { name: "Extracted Information" }),
    ).toBeDefined();
    expect(screen.getByText("account_status")).toBeDefined();
    expect(screen.getByText('"active"')).toBeDefined();
    expect(screen.queryByText("raw_code_output")).toBeNull();
    expect(screen.queryByText('"debug payload"')).toBeNull();
  });

  it("renders a string code block extraction without dropping it", () => {
    const block = buildBlock({
      block_type: "code",
      label: "collect_summary",
      output: {
        extracted_information: "Order #1024 shipped on Tuesday",
      },
    });

    render(<WorkflowRunBlockDetail activeItem={block} timeline={[]} />);

    expect(
      screen.getByRole("tab", { name: "Extracted Information" }),
    ).toBeDefined();
    expect(screen.getByText('"Order #1024 shipped on Tuesday"')).toBeDefined();
  });

  it("keeps a null code block extraction visible in the detail panel", () => {
    const block = buildBlock({
      block_type: "code",
      label: "collect_empty",
      output: {
        extracted_information: null,
      },
    });

    render(<WorkflowRunBlockDetail activeItem={block} timeline={[]} />);

    const tab = screen.getByRole("tab", { name: "Extracted Information" });
    expect(tab.hasAttribute("disabled")).toBe(false);
    expect(tab.getAttribute("data-state")).toBe("active");
    expect(screen.getByText("null")).toBeDefined();
    expect(screen.queryByText("No block output.")).toBeNull();
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

  it("shows a diagnostics link in the inspector for any block with a task id", () => {
    const block = buildBlock({
      block_type: "file_download",
      task_id: "tsk_123",
    });

    render(
      <MemoryRouter>
        <WorkflowRunBlockDetail activeItem={block} timeline={[]} />
      </MemoryRouter>,
    );

    const diagnosticsLink = screen.getByRole("link", {
      name: /diagnostics/i,
    });
    expect(diagnosticsLink.getAttribute("href")).toBe(
      "/tasks/tsk_123/diagnostics",
    );
  });

  it("opens diagnostics on the selected action step when the detail panel is focused on an action", () => {
    const action = buildAction({
      action_id: "act_code_step",
      step_id: "stp_code_2",
      step_order: 2,
    });

    expect(renderActionDiagnosticsHref(action, [action])).toBe(
      "/tasks/tsk_code/diagnostics?step_id=stp_code_2&step=2",
    );
  });

  it("falls back to a step query when the selected action has no step id", () => {
    const action = buildAction({
      action_id: "act_legacy_step",
      step_order: 2,
    });

    expect(renderActionDiagnosticsHref(action, [action])).toBe(
      "/tasks/tsk_code/diagnostics?step=2",
    );
  });

  it("uses the selected retry's step index as the diagnostics fallback", () => {
    const originalAttempt = buildAction({
      action_id: "act_retry_original",
      status: Status.Failed,
      step_id: "stp_code_order_1_original",
      step_order: 1,
    });
    const selectedRetry = buildAction({
      action_id: "act_retry_selected",
      step_id: "stp_code_order_1_stale",
      step_order: 1,
    });

    expect(
      renderActionDiagnosticsHref(selectedRetry, [
        {
          ...originalAttempt,
          action_id: "act_order_0",
          step_id: "stp_code_order_0",
          step_order: 0,
          status: Status.Completed,
        },
        originalAttempt,
        selectedRetry,
      ]),
    ).toBe("/tasks/tsk_code/diagnostics?step_id=stp_code_order_1_stale&step=2");
  });

  it("does not count non-contiguous actions from the same step as retries", () => {
    const stepZeroStart = buildAction({
      action_id: "act_order_0_start",
      step_id: "stp_code_order_0",
      step_order: 0,
      action_order: 0,
    });
    const stepOneStart = buildAction({
      action_id: "act_order_1_start",
      step_id: "stp_code_order_1",
      step_order: 1,
      action_order: 0,
    });
    const stepZeroFollowUp = buildAction({
      action_id: "act_order_0_follow_up",
      step_id: "stp_code_order_0",
      step_order: 0,
      action_order: 1,
    });
    const selectedAction = buildAction({
      action_id: "act_order_1_selected",
      step_id: "stp_code_order_1",
      step_order: 1,
      action_order: 1,
    });

    expect(
      renderActionDiagnosticsHref(selectedAction, [
        stepZeroStart,
        stepOneStart,
        stepZeroFollowUp,
        selectedAction,
      ]),
    ).toBe("/tasks/tsk_code/diagnostics?step_id=stp_code_order_1&step=1");
  });

  it("ignores later step actions when computing the diagnostics fallback", () => {
    const stepStart = buildAction({
      action_id: "act_step_start",
      step_order: 1,
      action_order: 0,
    });
    const laterStepAction = buildAction({
      action_id: "act_later_step",
      step_order: 2,
      action_order: 0,
    });
    const selectedAction = buildAction({
      action_id: "act_step_selected",
      step_order: 1,
      action_order: 1,
    });

    expect(
      renderActionDiagnosticsHref(selectedAction, [
        buildAction({
          action_id: "act_order_0",
          step_order: 0,
          action_order: 0,
        }),
        stepStart,
        laterStepAction,
        selectedAction,
      ]),
    ).toBe("/tasks/tsk_code/diagnostics?step=1");
  });

  it("uses action ordering for fallback when only one same-step action has a step id", () => {
    const stepStart = buildAction({
      action_id: "act_modern_step_start",
      step_id: "stp_code_order_1",
      step_order: 1,
      action_order: 0,
    });
    const selectedAction = buildAction({
      action_id: "act_legacy_step_selected",
      step_id: null,
      step_order: 1,
      action_order: 1,
    });

    expect(
      renderActionDiagnosticsHref(selectedAction, [
        buildAction({
          action_id: "act_order_0",
          step_order: 0,
          action_order: 0,
        }),
        stepStart,
        selectedAction,
      ]),
    ).toBe("/tasks/tsk_code/diagnostics?step=1");
  });

  it("does not duplicate the timeline with an Actions (N) section", () => {
    const action: ActionsApiResponse = {
      action_id: "act_download",
      action_type: ActionTypes.Click,
      status: Status.Completed,
      task_id: null,
      step_id: null,
      step_order: null,
      action_order: null,
      reasoning: "Click the invoice download link",
      description: null,
      intention: null,
      response: null,
      text: null,
      created_by: null,
      confidence_float: 1,
    };
    const block = buildBlock({
      block_type: "file_download",
      label: "download_invoice",
      actions: [action],
    });

    render(<WorkflowRunBlockDetail activeItem={block} timeline={[]} />);

    // The bottom timeline already lists every action; the detail panel must
    // not re-list them under an "Actions (N)" section.
    expect(screen.queryByText(/^Actions \(\d+\)$/)).toBeNull();
    expect(screen.queryByText("Click the invoice download link")).toBeNull();
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

  it("does not show a redundant Selected action section when a child action is selected", () => {
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

    // The block header still identifies the resolved block...
    expect(screen.getByText("calendar_lookup")).toBeDefined();
    // ...but the redundant "Selected action" card is gone.
    expect(screen.queryByText("Selected action")).toBeNull();
  });

  it("reflects the selected child action's data in the inspector tabs, not the parent block", () => {
    const action: ActionsApiResponse = {
      action_id: "act_input",
      action_type: ActionTypes.InputText,
      status: Status.Completed,
      task_id: null,
      step_id: null,
      step_order: null,
      action_order: null,
      reasoning: "Input the last name into the search field",
      description: null,
      intention: null,
      response: null,
      text: "McTesterson",
      created_by: null,
      confidence_float: 1,
    };
    const block = buildBlock({
      workflow_run_block_id: "wrb_task",
      block_type: "task_v2",
      label: "registry_search",
      actions: [action],
    });

    render(
      <WorkflowRunBlockDetail
        activeItem={action}
        timeline={[buildBlockItem(block)]}
      />,
    );

    // The action's reasoning is surfaced in the inspector (Summary by default).
    expect(
      screen.getByText("Input the last name into the search field"),
    ).toBeDefined();
    // Its input value shows under Inputs — the child action, not the block.
    // Radix Tabs activates on mousedown, not a bare click event.
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Inputs" }));
    expect(screen.getByText("McTesterson")).toBeDefined();
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
