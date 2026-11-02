import {
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  test,
  vi,
} from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { ReactFlowProvider } from "@xyflow/react";
import { MemoryRouter } from "react-router-dom";

import { parse as parseYaml } from "yaml";
import { ProxyLocation } from "@/api/types";
import * as axiosClientModule from "@/api/AxiosClient";
import { useCopilotProposalStore } from "@/store/CopilotProposalStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

import {
  EMPTY_NARRATIVE,
  type ReviewProjection,
} from "../../copilot/narrativeState";
import { ReviewGateCard } from "../../copilot/cards/ReviewGateCard";
import type {
  WorkflowApiResponse,
  WorkflowBlock,
  WorkflowSettings,
} from "../../types/workflowTypes";
import { diffWorkflowVersions } from "../panels/workflowReviewDiff";
import { getElements } from "../workflowEditorUtils";
import { buildDocProposal } from "./docProposal";
import { DocProposalProvider } from "./DocProposalView";
import { DocScope } from "./DocScope";
import { WorkflowDocEditor } from "./WorkflowDocEditor";

// The run widgets need studio run contexts; this file tests the proposal, so they are stubbed.
vi.mock("./DocStepResult", () => ({ DocStepResult: () => null }));
vi.mock("./DocLiveIndicator", () => ({ DocLiveIndicator: () => null }));
vi.mock("./DocRunStepButton", () => ({ DocRunStepButton: () => null }));

vi.mock("./useDocRun", () => {
  const blocksByLabel = new Map();
  return { useDocRun: () => ({ runId: null, blocksByLabel, live: false }) };
});

async function renderInStudio(ui: ReactElement) {
  let view!: ReturnType<typeof render>;
  await act(async () => {
    view = render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <ReactFlowProvider>{ui}</ReactFlowProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  });
  return view;
}

const settings = {
  proxyLocation: ProxyLocation.Residential,
  finallyBlockLabel: null,
} as unknown as WorkflowSettings;

const task = (
  label: string,
  next: string | null,
  extra: Record<string, unknown> = {},
) =>
  ({
    label,
    block_type: "navigation",
    next_block_label: next,
    continue_on_failure: false,
    model: null,
    navigation_goal: `Goal of ${label}`,
    output_parameter: {
      parameter_type: "output",
      key: `${label}_output`,
      output_parameter_id: `op-${label}`,
    },
    ...extra,
  }) as unknown as WorkflowBlock;

const workflow = (
  blocks: WorkflowBlock[],
  overrides: Partial<WorkflowApiResponse> = {},
) =>
  ({
    workflow_permanent_id: "wpid_test",
    workflow_id: "w_test",
    title: "Synthetic",
    description: null,
    proxy_location: ProxyLocation.Residential,
    ...overrides,
    workflow_definition: { parameters: [], blocks },
  }) as unknown as WorkflowApiResponse;

const saved = [
  task("open_page", "old_step", { notes: "Use the search box." }),
  task("old_step", "fill_form"),
  task("fill_form", "read_total", {
    notes: "Skip optional fields.",
    continue_on_failure: null,
    disable_cache: null,
    ignore_workflow_system_prompt: null,
    next_loop_on_failure: null,
    max_retries: null,
    complete_on_download: null,
    complete_verification: null,
    include_action_history_in_verification: null,
    include_extracted_text: null,
  }),
  task("read_total", null, { notes: "Totals sit at the bottom." }),
];
const proposed = [
  task("open_page", "fill_form", {
    notes: "Use the search box at the top.",
    navigation_goal: "Open the page",
  }),
  task("fill_form", "read_total", {
    notes: "Skip optional fields.",
    continue_on_failure: false,
    disable_cache: false,
    ignore_workflow_system_prompt: false,
    next_loop_on_failure: false,
    max_retries: 0,
    complete_on_download: false,
    complete_verification: true,
    include_action_history_in_verification: false,
    include_extracted_text: true,
  }),
  task("read_total", null),
];
const review: ReviewProjection = {
  blocks: [
    { label: "open_page", blockType: "navigation", change: "changed" },
    { label: "old_step", blockType: "navigation", change: "removed" },
    { label: "read_total", blockType: "navigation", change: "changed" },
  ],
  duplicateWrites: [],
  keptNotes: ["fill_form"],
  keptDescriptions: [],
};

async function renderDoc(actionable = true, acceptable = actionable) {
  const decide = vi.fn(async () => true);
  useCopilotProposalStore.getState().setProposal({
    proposed: workflow(proposed),
    baseline: workflow(saved),
    review,
    actionable,
    acceptable,
    decide,
  });
  const live = getElements(saved, settings, true);
  await renderInStudio(
    <WorkflowDocEditor nodes={live.nodes} edges={live.edges} />,
  );
  return decide;
}

beforeAll(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
});

beforeEach(() => {
  vi.spyOn(axiosClientModule, "getClient").mockResolvedValue({
    post: vi.fn(async (_url, request) => ({
      data: {
        workflow_definition: parseYaml(request.workflow_definition_yaml),
      },
    })),
  } as never);
});

afterEach(() => {
  cleanup();
  useCopilotProposalStore.getState().setProposal(null);
  vi.restoreAllMocks();
});

describe("Copilot proposal in the Doc view", () => {
  test("renders what Accept would save, with changed, removed and note changes marked", async () => {
    await renderDoc();

    expect(screen.getByText("Open the page")).not.toBeNull();
    expect(screen.getAllByText("Changed").length).toBeGreaterThanOrEqual(2);
    const removed = document.querySelector('[data-doc-removed="old_step"]');
    expect(removed?.textContent).toContain("old_step");
    expect(removed?.querySelector(".line-through")).not.toBeNull();
    // The edited note shows its old text struck through; the dropped one shows as removed.
    expect(screen.getByText("Use the search box at the top.")).not.toBeNull();
    expect(
      screen.getByText("Use the search box.").closest(".line-through"),
    ).not.toBeNull();
    expect(
      screen.getByText("Totals sit at the bottom.").closest(".line-through"),
    ).not.toBeNull();
  });

  test("shows workflow-setting and description changes in the proposal", async () => {
    const proposedWorkflow = workflow(proposed, {
      browser_type: "chromium",
      webhook_callback_url: "new-webhook-token",
    });
    const baselineWorkflow = workflow(saved, {
      browser_type: null,
      description: "The workflow gathers and verifies the requested data.",
      webhook_callback_url: "old-webhook-token",
    });
    useCopilotProposalStore.getState().setProposal({
      proposed: proposedWorkflow,
      baseline: baselineWorkflow,
      review,
      actionable: true,
      acceptable: true,
      decide: vi.fn(async () => true),
    });
    const live = getElements(saved, settings, true);
    await renderInStudio(
      <WorkflowDocEditor nodes={live.nodes} edges={live.edges} />,
    );

    const card = screen.getByRole("region", { name: "Copilot proposal" });
    expect(card.textContent).toContain("2 workflow settings changed");
    expect(card.textContent).toContain("workflow description removed");
    fireEvent.click(within(card).getByRole("button", { expanded: false }));
    expect(card.textContent).toContain("Browser type: Not set → chromium");
    expect(card.textContent).toContain(
      "Webhook callback URL: old-webhook-token → new-webhook-token",
    );
    expect(
      screen
        .getByText("The workflow gathers and verifies the requested data.")
        .closest(".line-through"),
    ).not.toBeNull();
  });

  test("shows the same run coverage and duplicate-write evidence as the chat card", async () => {
    const evidenceReview: ReviewProjection = {
      blocks: [
        {
          label: "open_page",
          blockType: "navigation",
          change: "changed",
          neverTested: true,
          coverage: "different_source",
        },
        {
          label: "fill_form",
          blockType: "task",
          change: "changed",
          neverTested: true,
          coverage: "never_run",
        },
        {
          label: "read_total",
          blockType: "navigation",
          change: "changed",
          neverTested: true,
          coverage: "unknown",
        },
      ],
      duplicateWrites: [
        {
          blockType: "google_sheets_write",
          blockLabels: ["write_first", "write_second"],
        },
      ],
    };
    useCopilotProposalStore.getState().setProposal({
      proposed: workflow(proposed),
      baseline: workflow(saved),
      review: evidenceReview,
      tested: false,
      actionable: true,
      acceptable: true,
      decide: vi.fn(async () => true),
    });
    const live = getElements(saved, settings, true);
    const noop = () => {};

    await renderInStudio(
      <>
        <WorkflowDocEditor nodes={live.nodes} edges={live.edges} />
        <ReviewGateCard
          turn={{
            ...EMPTY_NARRATIVE,
            turnId: "turn-evidence",
            turnIndex: 1,
            designStarted: true,
            designEnded: true,
            draft: { blockCount: 3, blockLabels: [], summary: null },
            review: evidenceReview,
            terminal: "response",
          }}
          pending
          verdict="untested"
          actionsEnabled
          hasProposal
          onAccept={noop}
          onAlwaysAccept={noop}
          onReject={noop}
          onReview={noop}
          latestTurnCause={null}
        />
      </>,
    );

    fireEvent.click(screen.getByRole("button", { name: /Copilot proposal/ }));

    for (const label of [
      "Different source",
      "Never tested",
      "Not tested under this name",
      "Write First and Write Second write to the same destination.",
    ]) {
      expect(screen.getAllByText(label)).toHaveLength(2);
    }
  });

  test("shows added, removed, and changed workflow inputs in the proposal", async () => {
    const baseline = workflow([]);
    baseline.workflow_definition.parameters = [
      {
        parameter_type: "workflow",
        workflow_parameter_type: "string",
        key: "account",
        default_value: "old value",
      },
      {
        parameter_type: "workflow",
        workflow_parameter_type: "string",
        key: "removed_input",
        default_value: null,
      },
    ] as WorkflowApiResponse["workflow_definition"]["parameters"];
    const proposedWorkflow = workflow([]);
    proposedWorkflow.workflow_definition.parameters = [
      {
        parameter_type: "workflow",
        workflow_parameter_type: "string",
        key: "account",
        default_value: "new value",
      },
      {
        parameter_type: "workflow",
        workflow_parameter_type: "integer",
        key: "added_input",
        default_value: null,
      },
    ] as WorkflowApiResponse["workflow_definition"]["parameters"];
    const comparison = diffWorkflowVersions(baseline, proposedWorkflow);
    useCopilotProposalStore.getState().setProposal({
      proposed: proposedWorkflow,
      baseline,
      review,
      actionable: true,
      acceptable: true,
      decide: vi.fn(async () => true),
    });
    const live = getElements([], settings, true);
    await renderInStudio(
      <WorkflowDocEditor nodes={live.nodes} edges={live.edges} />,
    );

    const card = screen.getByRole("region", { name: "Copilot proposal" });
    expect(card.textContent).toContain("3 inputs changed");
    fireEvent.click(within(card).getByRole("button", { expanded: false }));
    const rows = [...card.querySelectorAll("li")];
    expect(rows).toHaveLength(comparison.inputs.length);
    comparison.inputs.forEach((input) => {
      const row = rows.find((item) => item.textContent?.includes(input.key));
      expect(row).toBeDefined();
      expect(row?.textContent).toContain(
        input.status === "new"
          ? "Added"
          : input.status === "removed"
            ? "Removed"
            : "Changed",
      );
      input.changes.forEach((change) => {
        expect(row?.textContent).toContain(change.label);
        expect(row?.textContent).toContain(
          `${change.before ?? "Not set"} → ${change.after ?? "Not set"}`,
        );
      });
    });
  });

  test("uses live blocks when an unstaged proposal has no save-data baseline", async () => {
    const previousGetSaveData =
      useWorkflowHasChangesStore.getState().getSaveData;
    const baselineBlocks = [
      task("open_page", null, { navigation_goal: "Open the saved page." }),
    ];
    const proposedBlocks = [
      task("open_page", null, { navigation_goal: "Open the proposed page." }),
    ];
    useWorkflowHasChangesStore.setState({ getSaveData: () => null });
    useCopilotProposalStore.getState().setProposal({
      proposed: workflow(proposedBlocks),
      baseline: null,
      review,
      actionable: true,
      acceptable: true,
      decide: vi.fn(async () => true),
    });

    try {
      const live = getElements(baselineBlocks, settings, true);
      await renderInStudio(
        <WorkflowDocEditor nodes={live.nodes} edges={live.edges} />,
      );
      const card = screen.getByRole("region", { name: "Copilot proposal" });
      expect(card.textContent).toContain("1 step changed");
      expect(screen.getByText("Open the proposed page.")).not.toBeNull();
    } finally {
      cleanup();
      useWorkflowHasChangesStore.setState({ getSaveData: previousGetSaveData });
    }
  });

  test("renders a removed first branch step with a regenerated branch ID", async () => {
    const conditional = (ifTarget: string, branchId?: string) =>
      ({
        label: "check",
        block_type: "conditional",
        next_block_label: "after",
        branch_conditions: [
          {
            ...(branchId && { id: branchId }),
            criteria: {
              criteria_type: "jinja2_template",
              expression: "{{ ready }}",
            },
            next_block_label: ifTarget,
            is_default: false,
          },
          {
            id: "otherwise",
            criteria: null,
            next_block_label: "right",
            is_default: true,
          },
        ],
      }) as unknown as WorkflowBlock;
    const baselineBlocks = [
      conditional("gone", "if-before"),
      task("gone", "right"),
      task("right", "after"),
      task("after", null),
    ];
    const proposedBlocks = [
      conditional("right"),
      task("right", "after"),
      task("after", null),
    ];
    useCopilotProposalStore.getState().setProposal({
      proposed: workflow(proposedBlocks),
      baseline: workflow(baselineBlocks),
      review: {
        ...review,
        blocks: [
          { label: "check", blockType: "conditional", change: "changed" },
          { label: "gone", blockType: "navigation", change: "removed" },
        ],
      },
      actionable: true,
      acceptable: true,
      decide: vi.fn(async () => true),
    });
    const live = getElements(baselineBlocks, settings, true);
    await renderInStudio(
      <WorkflowDocEditor nodes={live.nodes} edges={live.edges} />,
    );

    const branches = document.querySelectorAll('[role="tabpanel"]');
    expect(
      [...branches].some((branch) =>
        branch.querySelector('[data-doc-removed="gone"]'),
      ),
    ).toBe(true);
  });

  test("renders an orphaned loop child inside its surviving loop", async () => {
    const loop = (loopBlocks: WorkflowBlock[]) =>
      ({
        label: "each_item",
        block_type: "for_loop",
        loop_variable_reference: "items",
        loop_blocks: loopBlocks,
        next_block_label: null,
      }) as unknown as WorkflowBlock;
    const baselineBlocks = [loop([task("only", null)])];
    const proposedBlocks = [loop([])];
    useCopilotProposalStore.getState().setProposal({
      proposed: workflow(proposedBlocks),
      baseline: workflow(baselineBlocks),
      review: {
        ...review,
        blocks: [{ label: "only", blockType: "navigation", change: "removed" }],
      },
      actionable: true,
      acceptable: true,
      decide: vi.fn(async () => true),
    });
    const live = getElements(baselineBlocks, settings, true);
    await renderInStudio(
      <WorkflowDocEditor nodes={live.nodes} edges={live.edges} />,
    );

    const removed = document.querySelector('[data-doc-removed="only"]');
    expect(removed?.closest("[data-doc-node-id]")?.textContent).toContain(
      "No steps in this loop yet",
    );
  });

  test("does not validate live save data against a pending proposal", async () => {
    vi.useFakeTimers();
    const post = vi.fn(
      async (_url: string, request: { workflow_definition_yaml: string }) => ({
        data: {
          workflow_definition: parseYaml(request.workflow_definition_yaml),
        },
      }),
    );
    vi.spyOn(axiosClientModule, "getClient").mockResolvedValue({
      post,
    } as never);
    const getSaveData = useWorkflowHasChangesStore.getState().getSaveData;
    // A save request that builds, so the only thing stopping validation is the pending proposal.
    useWorkflowHasChangesStore.setState({
      getSaveData: () =>
        ({
          title: "Doc",
          description: "",
          settings: { proxyLocation: ProxyLocation.Residential },
          parameters: [],
          blocks: [],
          workflow: { is_saved_task: false, status: "published" },
        }) as never,
    });
    try {
      await renderDoc();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      expect(
        post.mock.calls.filter(([url]) => url === "/workflows/validate"),
      ).toEqual([]);
    } finally {
      useWorkflowHasChangesStore.setState({ getSaveData });
      vi.useRealTimers();
      vi.restoreAllMocks();
    }
  });

  test("collapses to one header line and lists kept notes when opened", async () => {
    expect(
      diffWorkflowVersions(workflow(saved), workflow(proposed)).merged.get(
        "fill_form",
      )?.status,
    ).toBe("unchanged");
    await renderDoc();
    const card = screen.getByRole("region", { name: "Copilot proposal" });

    expect(card.textContent).toContain("2 steps changed · 1 removed");
    expect(card.textContent).not.toContain("Kept your notes");
    fireEvent.click(within(card).getByRole("button", { expanded: false }));
    expect(card.textContent).toContain("Kept your notes on Fill Form.");
    // Step names read as the chat's review card shows them, not as raw labels.
    expect(within(card).getByText("Open Page").getAttribute("title")).toBe(
      "open_page",
    );
    expect(
      within(card).getByText("Old Step").closest(".line-through"),
    ).not.toBeNull();
    // No whole-turn verdict anywhere on the proposal.
    expect(card.textContent).not.toMatch(/Done|✓|Tested/);
  });

  test("Accept and Reject settle the whole proposal through the chat", async () => {
    const decide = await renderDoc();

    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    expect(decide).toHaveBeenCalledWith("approve");
  });

  test("holds Accept and Reject while the chat would not act", async () => {
    const decide = await renderDoc(false);

    const accept = screen.getByRole("button", { name: "Accept" });
    expect((accept as HTMLButtonElement).disabled).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Reject" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    fireEvent.click(accept);
    expect(decide).not.toHaveBeenCalled();
  });

  test("holds only Accept while the chat is turning auto-accept off", async () => {
    await renderDoc(true, false);

    expect(
      (screen.getByRole("button", { name: "Accept" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Reject" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });

  test("uses the comparison diff's changed set and settings rows", async () => {
    const conditional = (
      ifTarget: string,
      otherwiseTarget: string,
      next: string,
    ) =>
      ({
        label: "check",
        block_type: "conditional",
        next_block_label: next,
        branch_conditions: [
          {
            id: "if",
            criteria: {
              criteria_type: "jinja2_template",
              expression: "{{ x }}",
            },
            next_block_label: ifTarget,
            is_default: false,
          },
          {
            id: "otherwise",
            criteria: null,
            next_block_label: otherwiseTarget,
            is_default: true,
          },
        ],
      }) as unknown as WorkflowBlock;
    const baselineBlocks = [
      conditional("E", "B", "M"),
      task("E", "M"),
      task("B", "X"),
      task("X", "M"),
      task("M", null),
    ];
    const proposedBlocks = [
      conditional("E", "B", "X"),
      task("E", "X"),
      task("B", "M"),
      task("X", "M"),
      task("M", null),
    ];
    const baseline = workflow(baselineBlocks, {
      title: "Saved workflow title",
      run_with: "agent",
      code_version: null,
    });
    const proposedWorkflow = workflow(proposedBlocks, {
      title: "Proposed workflow title",
      browser_type: "chromium",
      run_with: "agent",
      code_version: 2,
    });
    const comparison = diffWorkflowVersions(baseline, proposedWorkflow);
    const expectedMarks = [...comparison.after]
      .filter(
        ([, change]) => change.status === "new" || change.status === "changed",
      )
      .map(([label]) => label)
      .sort();
    const expectedSettings = comparison.settings
      .filter((change) => change.key !== "description")
      .map(
        (change) =>
          `${change.label}: ${change.before ?? "Not set"} → ${change.after ?? "Not set"}`,
      );
    useCopilotProposalStore.getState().setProposal({
      proposed: proposedWorkflow,
      baseline,
      review,
      actionable: true,
      acceptable: true,
      decide: vi.fn(async () => true),
    });
    const live = getElements(baselineBlocks, settings, true);
    await renderInStudio(
      <WorkflowDocEditor nodes={live.nodes} edges={live.edges} />,
    );
    const card = screen.getByRole("region", { name: "Copilot proposal" });
    fireEvent.click(within(card).getByRole("button", { expanded: false }));
    const renderedMarks = [...card.querySelectorAll("button")]
      .map((button) =>
        button.querySelector("span[title]")?.getAttribute("title"),
      )
      .filter((label): label is string => typeof label === "string")
      .sort();
    const renderedSettings = [...card.querySelectorAll("li")].map(
      (row) => row.textContent,
    );
    expect(renderedMarks).toEqual(expectedMarks);
    expect(renderedSettings).toEqual(expectedSettings);
    expect(expectedMarks).toEqual(expect.arrayContaining(["B", "E", "check"]));
    expect(comparison.settings.map((change) => change.key)).toContain("title");
    expect(comparison.settings.map((change) => change.key)).not.toContain(
      "code_version",
    );
    expect(card.textContent).toContain("Proposed workflow title");
  });

  test("swallows undo and redo while a proposal is shown, wherever focus is, but not in text fields or the live browser", async () => {
    await renderDoc();
    const historyListener = vi.fn();
    document.addEventListener("keydown", historyListener);
    const composer = document.createElement("textarea");
    const browserCanvas = document.createElement("canvas");
    document.body.append(composer, browserCanvas);
    try {
      const region = screen.getByRole("region", { name: "Workflow document" });
      for (const target of [region, document.body]) {
        for (const modifiers of [
          { key: "z", ctrlKey: true },
          { key: "z", metaKey: true, shiftKey: true },
          { key: "y", ctrlKey: true },
        ]) {
          const event = new KeyboardEvent("keydown", {
            bubbles: true,
            cancelable: true,
            ...modifiers,
          });
          fireEvent(target, event);
          expect(event.defaultPrevented).toBe(true);
        }
      }
      expect(historyListener).not.toHaveBeenCalled();
      const typing = new KeyboardEvent("keydown", {
        key: "z",
        ctrlKey: true,
        bubbles: true,
        cancelable: true,
      });
      fireEvent(composer, typing);
      expect(typing.defaultPrevented).toBe(false);
      const remoteUndo = new KeyboardEvent("keydown", {
        key: "z",
        ctrlKey: true,
        bubbles: true,
        cancelable: true,
      });
      fireEvent(browserCanvas, remoteUndo);
      expect(remoteUndo.defaultPrevented).toBe(false);
    } finally {
      composer.remove();
      browserCanvas.remove();
      document.removeEventListener("keydown", historyListener);
    }
  });

  test("hides the Save bar while the proposal is pending, so Accept is the only decision", async () => {
    act(() => useWorkflowHasChangesStore.getState().setHasChanges(true));
    await renderDoc();
    expect(
      screen.getByRole("region", { name: "Copilot proposal" }),
    ).not.toBeNull();
    expect(screen.queryByText("Unsaved changes")).toBe(null);
    expect(screen.queryByRole("button", { name: "Save" })).toBe(null);
    act(() => useWorkflowHasChangesStore.getState().setHasChanges(false));
  });

  test("shows the editable doc again once the proposal clears", async () => {
    await renderDoc();
    act(() => useCopilotProposalStore.getState().setProposal(null));

    expect(screen.queryByRole("region", { name: "Copilot proposal" })).toBe(
      null,
    );
    expect(screen.getByText("Goal of old_step")).not.toBeNull();
  });
});

describe("Copilot proposal marks", () => {
  const reviewProjection = (label: string) => ({
    blocks: [{ label, blockType: "navigation", change: "changed" as const }],
    duplicateWrites: [],
    keptNotes: [],
    keptDescriptions: [],
  });
  const build = (baselineBlocks: unknown[], proposedBlocks: unknown[]) => {
    const baseline = workflow(baselineBlocks as WorkflowBlock[]);
    const proposedWorkflow = workflow(proposedBlocks as WorkflowBlock[]);
    return buildDocProposal({
      nodes: [],
      edges: [],
      baselineBlocks,
      proposedBlocks,
      diff: diffWorkflowVersions(baseline, proposedWorkflow),
      review: reviewProjection("a"),
      description: proposedWorkflow.description,
      parameters: [],
    });
  };

  test("shows a same-label type change as removed and added", () => {
    const proposal = build(
      [task("step", null, { block_type: "text_prompt", prompt: "Old" })],
      [
        task("step", null, {
          block_type: "extraction",
          data_extraction_goal: "New",
        }),
      ],
    );

    expect(proposal.marks.get("step")).toBe("added");
    expect(proposal.removed).toEqual([
      expect.objectContaining({ label: "step", blockType: "text_prompt" }),
    ]);
  });

  test("shows a step moved into a new loop at its old position", () => {
    const proposal = build(
      [task("a", "b"), task("b", null)],
      [
        {
          label: "new_loop",
          block_type: "for_loop",
          loop_variable_reference: "rows",
          loop_blocks: [task("a", null)],
          next_block_label: "b",
        },
        task("b", null),
      ],
    );

    expect(proposal.marks.get("a")).toBe("changed");
    expect(proposal.removed).toEqual([
      expect.objectContaining({
        label: "a",
        anchor: { label: "b", side: "before" },
      }),
    ]);
  });

  test("a conditional whose branch targets swap stays marked changed", () => {
    const cond = (ifTarget: string, otherwiseTarget: string) => ({
      label: "check",
      block_type: "conditional",
      next_block_label: "done",
      branch_conditions: [
        {
          id: "b-if",
          criteria: { criteria_type: "jinja2_template", expression: "{{ x }}" },
          next_block_label: ifTarget,
          is_default: false,
        },
        {
          id: "b-default",
          criteria: null,
          next_block_label: otherwiseTarget,
          is_default: true,
        },
      ],
    });
    const steps = [{ label: "left" }, { label: "right" }, { label: "done" }];
    const proposal = build(
      [cond("left", "right"), ...steps],
      [cond("right", "left"), ...steps],
    );

    expect(proposal.marks.get("check")).toBe("changed");
  });

  test("a removed branch step anchors inside its own branch, not beside another branch", () => {
    const branch = (label: string, next: string | null) => ({
      label,
      next_block_label: next,
    });
    const check = (ifTarget: string) => ({
      label: "check",
      block_type: "conditional",
      next_block_label: "right",
      branch_conditions: [
        { id: "1", is_default: false, next_block_label: ifTarget },
        { id: "2", is_default: true, next_block_label: "right" },
      ],
    });
    const proposal = build(
      [check("gone"), branch("gone", "right"), branch("right", null)],
      [check("right"), branch("right", null)],
    );

    expect(proposal.removed.map((r) => r.anchor)).toEqual([
      { label: "check", branchId: "1", side: "branch" },
    ]);
  });

  test("anchors a removed loop-only child inside its surviving loop", () => {
    const loop = (loop_blocks: unknown[]) => ({
      label: "each",
      block_type: "for_loop",
      loop_blocks,
    });
    const proposal = build(
      [loop([{ label: "only", block_type: "navigation" }])],
      [loop([])],
    );

    expect(proposal.removed[0]?.anchor).toEqual({
      label: "each",
      side: "loop",
    });
  });

  test("anchors a removed shared branch tail after its conditional", () => {
    const conditional = {
      label: "check",
      block_type: "conditional",
      next_block_label: null,
      branch_conditions: [
        { id: "if", is_default: false, next_block_label: "shared" },
        { id: "otherwise", is_default: true, next_block_label: "shared" },
      ],
    };
    const proposal = build(
      [conditional, { label: "shared", block_type: "navigation" }],
      [
        {
          ...conditional,
          branch_conditions: [
            { id: "if", is_default: false, next_block_label: null },
            { id: "otherwise", is_default: true, next_block_label: null },
          ],
        },
      ],
    );

    expect(proposal.removed[0]?.anchor).toEqual({
      label: "check",
      side: "after",
    });
  });

  test("a removed step after a conditional's branches stays after the conditional", () => {
    const check = (next: string | null, armNext: string | null) => [
      {
        label: "check",
        block_type: "conditional",
        next_block_label: next,
        branch_conditions: [
          { id: "if", is_default: false, next_block_label: "left" },
          { id: "otherwise", is_default: true, next_block_label: "right" },
        ],
      },
      { label: "left", block_type: "navigation", next_block_label: armNext },
      { label: "right", block_type: "navigation", next_block_label: armNext },
    ];
    const proposal = build(
      [
        ...check("merge", "merge"),
        { label: "merge", block_type: "navigation" },
      ],
      check(null, null),
    );

    expect(proposal.removed.map((r) => r.anchor)).toEqual([
      { label: "check", side: "after" },
    ]);
  });

  test("a removed first step stays in its branch when the branch is still there", () => {
    const check = (branches: unknown[]) => ({
      label: "check",
      block_type: "conditional",
      next_block_label: null,
      branch_conditions: branches,
    });
    const proposal = build(
      [
        check([
          {
            id: "c1",
            is_default: false,
            criteria: { expression: "{{ a }}" },
            next_block_label: "gone",
          },
          { id: "c2", is_default: true, next_block_label: null },
        ]),
        { label: "gone", block_type: "navigation" },
      ],
      [
        check([
          {
            id: "c1",
            is_default: false,
            criteria: { expression: "{{ b }}" },
            next_block_label: null,
          },
          {
            id: "c3",
            is_default: false,
            criteria: { expression: "{{ c }}" },
            next_block_label: null,
          },
          { id: "c2", is_default: true, next_block_label: null },
        ]),
      ],
    );

    expect(proposal.removed.map((r) => r.anchor)).toEqual([
      { label: "check", branchId: "c1", side: "branch" },
    ]);
  });

  test("steps of a branch Copilot dropped sit after the conditional", () => {
    const check = (branches: unknown[]) => ({
      label: "check",
      block_type: "conditional",
      next_block_label: null,
      branch_conditions: branches,
    });
    const proposal = build(
      [
        check([
          { id: "if", is_default: false, next_block_label: "gone" },
          { id: "otherwise", is_default: true, next_block_label: "kept" },
        ]),
        { label: "gone", block_type: "navigation" },
        { label: "kept", block_type: "navigation" },
      ],
      [
        check([
          { id: "otherwise", is_default: true, next_block_label: "kept" },
        ]),
        { label: "kept", block_type: "navigation" },
      ],
    );

    expect(proposal.removed.map((r) => r.anchor)).toEqual([
      { label: "check", side: "after" },
    ]);
  });

  test("a removed step keeps its trailing note and a scope-only move stays marked", () => {
    const loop = (inner: unknown[]) => ({
      label: "each",
      block_type: "for_loop",
      loop_blocks: inner,
    });
    const moved = { label: "a", block_type: "navigation" };
    const proposal = build(
      [
        loop([
          moved,
          { label: "b", block_type: "navigation", trailing_notes: "bye" },
        ]),
      ],
      [loop([]), moved],
    );

    expect(
      proposal.removed.find((step) => step.label === "b")?.trailingNotes,
    ).toBe("bye");
    expect(proposal.removed.find((step) => step.label === "a")?.anchor).toEqual(
      {
        label: "each",
        side: "loop",
      },
    );
    expect(proposal.marks.get("a")).toBe("changed");
  });

  test("uses the shared classifier when a terminal loop child moves to top level", () => {
    const loop = (loop_blocks: unknown[]) => ({
      label: "each_item",
      block_type: "for_loop",
      loop_blocks,
    });
    const child = { label: "inside", block_type: "navigation" };
    const proposal = build([loop([child])], [loop([]), child]);

    expect(proposal.marks.get("inside")).toBe("changed");
  });
});

test("marks steps rendered in an unplaced group", async () => {
  const proposedBlocks = [task("unplaced", null)];
  const proposedWorkflow = workflow(proposedBlocks);
  const baselineWorkflow = workflow([]);
  const live = getElements(proposedBlocks, settings, true);
  const blockNode = live.nodes.find((node) => node.type === "navigation");
  if (!blockNode) throw new Error("Expected a navigation node");
  const proposal = buildDocProposal({
    nodes: live.nodes,
    edges: live.edges,
    baselineBlocks: [],
    proposedBlocks,
    diff: diffWorkflowVersions(baselineWorkflow, proposedWorkflow),
    review: null,
    description: null,
    parameters: [],
  });

  await renderInStudio(
    <DocProposalProvider value={proposal}>
      <DocScope
        items={[
          {
            kind: "unplaced",
            nodeIds: [blockNode.id],
            readOnlyReason: "Test unplaced node",
          },
        ]}
        byId={new Map(live.nodes.map((node) => [node.id, node]))}
        noteProblems={new Map()}
      />
    </DocProposalProvider>,
  );

  expect(screen.getByText("Added")).toBeTruthy();
  expect(screen.getByText("Goal of unplaced")).toBeTruthy();
});

test("a proposal's input chips do not open the live Inputs panel", async () => {
  const withInput = workflow(proposed);
  withInput.workflow_definition.parameters = [
    {
      parameter_type: "workflow",
      workflow_parameter_type: "string",
      key: "account",
      default_value: null,
    },
  ] as unknown as WorkflowApiResponse["workflow_definition"]["parameters"];
  useCopilotProposalStore.getState().setProposal({
    proposed: withInput,
    baseline: workflow(saved),
    review,
    actionable: true,
    acceptable: true,
    decide: vi.fn(async () => true),
  });
  const live = getElements(saved, settings, true);
  await renderInStudio(
    <WorkflowDocEditor nodes={live.nodes} edges={live.edges} />,
  );

  const chip = screen.getByText("account");
  expect(chip.closest("button")).toBeNull();
  fireEvent.click(chip);
  expect(useWorkflowPanelStore.getState().workflowPanelState.active).toBe(
    false,
  );
});
