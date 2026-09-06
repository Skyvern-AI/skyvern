// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
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
  statusUnavailable: false,
  timelineQuery: vi.fn(),
}));

vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => ({
    data: mocks.workflowRun,
    isLoading: false,
    isError: mocks.statusUnavailable,
  }),
}));
vi.mock("../hooks/useWorkflowRunTimelineQuery", () => ({
  useWorkflowRunTimelineQuery: (options?: { workflowRunId?: string }) => {
    mocks.timelineQuery(options);
    return { data: mocks.timeline, isLoading: false };
  },
}));
// Radix ScrollArea needs ResizeObserver, which jsdom doesn't provide.
vi.mock("@/components/ui/scroll-area", () => ({
  ScrollArea: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  ScrollAreaViewport: ({ children }: { children?: ReactNode }) => (
    <div>{children}</div>
  ),
}));

// The search Popover + cmdk need ResizeObserver and scrollIntoView, which
// jsdom lacks. Install them for this suite only and restore afterward.
class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const originalScrollIntoView = Element.prototype.scrollIntoView;
let scrollIntoViewMock: ReturnType<typeof vi.fn>;

beforeAll(() => {
  vi.stubGlobal("ResizeObserver", MockResizeObserver);
});

afterAll(() => {
  vi.unstubAllGlobals();
  if (originalScrollIntoView) {
    Element.prototype.scrollIntoView = originalScrollIntoView;
  } else {
    delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
  }
});

beforeEach(() => {
  scrollIntoViewMock = vi.fn();
  Element.prototype.scrollIntoView =
    scrollIntoViewMock as unknown as typeof Element.prototype.scrollIntoView;
  // reduced-motion off by default; the jump uses smooth scrolling.
  window.matchMedia = vi
    .fn()
    .mockReturnValue({ matches: false }) as unknown as typeof window.matchMedia;
});

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

function renderTimeline(
  activeItem: WorkflowRunOverviewActiveElement,
  options: {
    hideBorder?: boolean;
    hideHeader?: boolean;
    onBlockItemSelected?: (block: WorkflowRunBlock) => void;
  } = {},
) {
  return render(
    <WorkflowRunTimeline
      workflowRunId={undefined}
      activeItem={activeItem}
      hideBorder={options.hideBorder}
      hideHeader={options.hideHeader}
      onLiveStreamSelected={noop}
      onActionItemSelected={noop}
      onBlockItemSelected={options.onBlockItemSelected ?? noop}
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
  // Restore before the setup file's afterEach awaits a real macrotask; a
  // failed assertion mid-test would otherwise leave fake timers installed
  // and hang that hook.
  vi.useRealTimers();
  cleanup();
  mocks.workflowRun = undefined;
  mocks.timeline = undefined;
  mocks.statusUnavailable = false;
  mocks.timelineQuery.mockClear();
});

describe("WorkflowRunTimeline", () => {
  it("does not offer a live stream from an unavailable status payload", () => {
    mocks.workflowRun = {
      status: Status.Running,
      total_steps: 0,
      credits_used: 0,
      cached_credits_used: 0,
      workflow: { workflow_definition: { finally_block_label: null } },
    };
    mocks.timeline = [];
    mocks.statusUnavailable = true;

    renderTimeline(null);
    expect(
      screen.queryByRole("button", {
        name: "Jump to the live stream of the running workflow",
      }),
    ).toBeNull();
  });

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

describe("timeline header counts", () => {
  function seed(totalSteps = 0, actions: WorkflowRunBlock["actions"] = null) {
    mocks.workflowRun = {
      status: Status.Completed,
      total_steps: totalSteps,
      credits_used: 0,
      cached_credits_used: 0,
      workflow: {
        workflow_definition: { blocks: [], finally_block_label: null },
      },
    };
    mocks.timeline = [
      buildBlockItem(
        buildBlock({ workflow_run_block_id: "wrb_a", label: "A", actions }),
      ),
    ];
  }

  // The header reports executed timeline blocks, not task steps.
  it("omits the steps chip when the run reports no steps", () => {
    seed();

    renderTimeline(null);

    const { container } = renderTimeline(null);

    expect(container.textContent).not.toContain("steps");
  });

  it("shows the executed block count instead of a steps chip", () => {
    seed(3, [{ action_id: "act_1" }] as unknown as WorkflowRunBlock["actions"]);

    const { container } = renderTimeline(null);

    expect(container.textContent).toContain("1 block");
    expect(container.textContent).toContain("1 action");
    expect(container.textContent).not.toContain("steps");
    // No configured blocks means no completed/configured ratio to explain, so
    // the metric stays plain text rather than an empty focus stop.
    const blockMetric = Array.from(container.querySelectorAll("span")).find(
      (el) => el.textContent === "1 block",
    );
    expect(blockMetric?.getAttribute("tabindex")).toBeNull();
  });
});

// The studio pane already paints this exact surface, so the card would draw a
// box inside its own fill; the legacy run view sits in a sidebar column on the
// page background, where the border is the only thing separating it. The two
// callers must stay divergent — collapsing them regresses one page or the other.
describe("timeline surface", () => {
  const CARD_CLASSES = ["border", "border-border", "bg-slate-elevation1"];

  function seed() {
    mocks.workflowRun = {
      status: Status.Completed,
      total_steps: 0,
      credits_used: 0,
      cached_credits_used: 0,
      workflow: {
        workflow_definition: { blocks: [], finally_block_label: null },
      },
    };
    mocks.timeline = [
      buildBlockItem(
        buildBlock({ workflow_run_block_id: "wrb_a", label: "A" }),
      ),
    ];
  }

  function timelineRoot(container: HTMLElement) {
    return container.firstElementChild as HTMLElement;
  }

  it("drops the card border and fill when hideBorder is set (studio)", () => {
    seed();
    const { container } = renderTimeline(null, { hideBorder: true });
    const root = timelineRoot(container);
    for (const cls of CARD_CLASSES) {
      expect(root.classList.contains(cls)).toBe(false);
    }
  });

  it("renders only the list when hideHeader is set", () => {
    seed();
    const { container } = renderTimeline(null, { hideHeader: true });
    const scope = within(container);
    expect(scope.queryByText("Timeline")).toBeNull();
    expect(
      scope.queryByText(
        (_, node) =>
          node?.tagName === "SPAN" &&
          /^\d+ blocks?$/.test(node.textContent ?? ""),
      ),
    ).toBeNull();
    expect(scope.queryByText(/credits$/)).toBeNull();
  });

  it("keeps the card border and fill when hideBorder is omitted (legacy parity)", () => {
    seed();
    const { container } = renderTimeline(null);
    const root = timelineRoot(container);
    for (const cls of CARD_CLASSES) {
      expect(root.classList.contains(cls)).toBe(true);
    }
  });
});

// The selection can land on a nested row from outside the timeline — the editor
// canvas pin, a deep link — so the reveal + scroll must not depend on the row
// being clickable (a collapsed ancestor unmounts it entirely).
describe("timeline selection reveal", () => {
  const child1 = buildBlock({
    workflow_run_block_id: "wrb_child_1",
    block_type: "navigation",
    label: "census_hold",
    parent_workflow_run_block_id: "wrb_container",
    created_at: "2026-01-01T00:00:10Z",
  });
  const child2 = buildBlock({
    workflow_run_block_id: "wrb_child_2",
    block_type: "navigation",
    label: "submit_form",
    parent_workflow_run_block_id: "wrb_container",
    created_at: "2026-01-01T00:00:20Z",
  });

  function seed() {
    mocks.workflowRun = {
      status: Status.Completed,
      total_steps: 0,
      credits_used: 0,
      cached_credits_used: 0,
      workflow: {
        workflow_definition: { blocks: [], finally_block_label: null },
      },
    };
    mocks.timeline = [
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_container",
          block_type: "task_v2",
          label: "container",
          created_at: "2026-01-01T00:00:00Z",
        }),
        [buildBlockItem(child1), buildBlockItem(child2)],
      ),
    ];
  }

  it("reveals and scrolls a nested row when the selection lands on it from outside", () => {
    vi.useFakeTimers();
    seed();

    const { rerender } = renderTimeline(null);
    // The container starts collapsed; the nested row is not mounted at all.
    expect(screen.queryByText("census_hold")).toBeNull();

    rerender(
      <WorkflowRunTimeline
        workflowRunId={undefined}
        activeItem={child1}
        onLiveStreamSelected={noop}
        onActionItemSelected={noop}
        onBlockItemSelected={noop}
        onThoughtItemSelected={noop}
        onIterationSelected={noop}
      />,
    );

    expect(screen.getByText("census_hold")).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(300);
    });
    const scrolled = scrollIntoViewMock.mock.contexts[0] as HTMLElement;
    expect(scrolled.textContent).toContain("census_hold");
  });

  it("a new selection into a container reopens it after a manual collapse", () => {
    vi.useFakeTimers();
    seed();

    const view = renderTimeline(child1);
    expect(screen.getByText("census_hold")).toBeTruthy();

    // The user hides the container; their choice holds for the CURRENT selection.
    fireEvent.click(screen.getByRole("button", { name: "Collapse" }));
    expect(screen.queryByText("census_hold")).toBeNull();

    // A different nested selection outranks the old collapse.
    view.rerender(
      <WorkflowRunTimeline
        workflowRunId={undefined}
        activeItem={child2}
        onLiveStreamSelected={noop}
        onActionItemSelected={noop}
        onBlockItemSelected={noop}
        onThoughtItemSelected={noop}
        onIterationSelected={noop}
      />,
    );

    expect(screen.getByText("submit_form")).toBeTruthy();
  });
});

describe("timeline run resolution", () => {
  it("states no run rather than deferring to the route when the prop is undefined", () => {
    renderTimeline(null);

    expect(mocks.timelineQuery).toHaveBeenCalledWith({
      workflowRunId: undefined,
    });
  });

  it("scopes the timeline to an explicit run id prop", () => {
    render(
      <WorkflowRunTimeline
        activeItem={null}
        workflowRunId="wr_explicit"
        onLiveStreamSelected={noop}
        onActionItemSelected={noop}
        onBlockItemSelected={noop}
        onThoughtItemSelected={noop}
        onIterationSelected={noop}
      />,
    );

    expect(mocks.timelineQuery).toHaveBeenCalledWith({
      workflowRunId: "wr_explicit",
    });
  });
});
