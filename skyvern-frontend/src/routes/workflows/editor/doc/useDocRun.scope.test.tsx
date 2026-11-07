// @vitest-environment jsdom
import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import {
  focusManager,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import { ActiveOrgContext } from "@/store/ActiveOrgContext";
import type { WorkflowRunTimelineItem } from "../../types/workflowRunTypes";
import { isStepRun, markStepRun, useDocRunStore } from "./docRunStore";
import { docLatestRunKey, useDocRun } from "./useDocRun";
import { stepShowsEarlierRun } from "./docRunState";
import { getElements } from "../workflowEditorUtils";
import { buildDocTree } from "./buildDocTree";
import { docBranchSelection } from "./docBranchSelection";
import { block, conditional, forLoop, settings } from "./docTestBlocks";
import { docRunFor } from "./useDocRunFor";

const timeline = vi.hoisted(() => ({
  data: [] as WorkflowRunTimelineItem[],
  isPlaceholderData: false,
  byId: {} as Record<string, unknown[]>,
}));
const urlRun = vi.hoisted(() => ({ id: "wr_1" as string | undefined }));
vi.mock("../../studio/useStudioRunId", () => ({
  useStudioRunId: () => urlRun.id,
}));
vi.mock("../../hooks/useWorkflowRunTimelineQuery", () => ({
  useWorkflowRunTimelineQuery: (options?: { workflowRunId?: string }) => {
    const rows = options?.workflowRunId && timeline.byId[options.workflowRunId];
    return rows ? { data: rows, isPlaceholderData: false } : timeline;
  },
}));
const runQuery = vi.hoisted(() => ({
  data: undefined as unknown,
  byId: {} as Record<string, unknown>,
  current: undefined as unknown,
}));
vi.mock("../../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: ({
    workflowRunId,
  }: {
    workflowRunId?: string;
  }) => ({
    data: (workflowRunId && runQuery.byId[workflowRunId]) ?? runQuery.data,
  }),
}));
vi.mock("../../hooks/useWorkflowQuery", () => ({
  useWorkflowQuery: () => ({ data: runQuery.current }),
}));
vi.mock("../../WorkflowPermanentIdContext", () => ({
  useWorkflowPermanentId: () => "wpid_1",
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => undefined,
}));
const client = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/api/AxiosClient", () => ({ getClient: async () => client }));

it("never retains another organization's rows for the same run ID", () => {
  timeline.data = [
    {
      type: "block",
      block: {
        workflow_run_id: "wr_1",
        label: "private_step",
        output: { value: "private result" },
      },
      children: [],
    },
  ] as unknown as WorkflowRunTimelineItem[];
  timeline.isPlaceholderData = false;
  let orgId = "org_a";
  function wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <ActiveOrgContext.Provider value={orgId}>
            {children}
          </ActiveOrgContext.Provider>
        </MemoryRouter>
      </QueryClientProvider>
    );
  }
  const { result, rerender } = renderHook(() => useDocRun(), { wrapper });
  expect(result.current.blocksByLabel.has("private_step")).toBe(true);

  timeline.isPlaceholderData = true;
  rerender();
  expect(result.current.blocksByLabel.has("private_step")).toBe(true);

  orgId = "org_b";
  rerender();
  expect(result.current.runId).toBe("wr_1");
  expect(result.current.blocksByLabel.size).toBe(0);
});

let stamp = 0;
// Each saved version stamps its own output parameter ids and times on every step.
const step = (label: string, extra: Record<string, unknown> = {}) => ({
  label,
  block_type: "extraction",
  ...extra,
  output_parameter: {
    key: `${label}_output`,
    output_parameter_id: `op_${++stamp}`,
    workflow_id: `w_${stamp}`,
    created_at: `t${stamp}`,
  },
});
const failedRow = (label: string): WorkflowRunTimelineItem =>
  ({
    type: "block",
    block: { workflow_run_id: "wr_1", label, status: "failed" },
    children: [],
  }) as unknown as WorkflowRunTimelineItem;

function renderEarlierRun(
  runBlocks: unknown[],
  currentBlocks: unknown[],
  runVersion = 1,
  parameters: [unknown[], unknown[]] = [[], []],
  current: Record<string, unknown> = {},
) {
  timeline.data = [failedRow("a"), failedRow("b")];
  timeline.isPlaceholderData = false;
  runQuery.data = {
    workflow: {
      version: runVersion,
      workflow_definition: { blocks: runBlocks, parameters: parameters[0] },
    },
  };
  runQuery.current = {
    version: 2,
    workflow_definition: { blocks: currentBlocks, parameters: parameters[1] },
    ...current,
  };
  return renderHook(() => useDocRun(), {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <ActiveOrgContext.Provider value="org_a">
            {children}
          </ActiveOrgContext.Provider>
        </MemoryRouter>
      </QueryClientProvider>
    ),
  }).result;
}

it("shows an earlier version's results by label but hides added and edited steps", () => {
  const result = renderEarlierRun(
    [
      step("a", { data_extraction_goal: "x" }),
      step("b", { data_extraction_goal: "old" }),
    ],
    [
      step("a", { data_extraction_goal: "x" }),
      step("b", { data_extraction_goal: "new" }),
      step("c"),
    ],
  );
  expect(result.current.earlierVersion).toBe(true);
  expect([...result.current.unknownLabels].sort()).toEqual(["b", "c"]);
  expect(result.current.blocksByLabel.has("a")).toBe(true);
});

it("keeps a test run's results after Accept saves the same steps as a newer version", () => {
  const result = renderEarlierRun(
    [
      step("a", { data_extraction_goal: "x", url: null }),
      step("b", { data_extraction_goal: "y" }),
    ],
    [
      step("a", { data_extraction_goal: "x" }),
      step("b", { data_extraction_goal: "y" }),
    ],
  );
  expect(result.current.earlierVersion).toBe(false);
  expect(result.current.unknownLabels.size).toBe(0);
  expect(result.current.blocksByLabel.has("a")).toBe(true);
});

it("marks results earlier once a step is edited or removed after Accept", () => {
  const edited = renderEarlierRun(
    [step("a", { data_extraction_goal: "x" })],
    [step("a", { data_extraction_goal: "z" })],
  );
  expect(edited.current.earlierVersion).toBe(true);
  const added = { key: "q", parameter_type: "workflow", default_value: "1" };
  const inputChanged = renderEarlierRun([step("a")], [step("a")], 1, [
    [],
    [added],
  ]);
  expect(inputChanged.current.earlierVersion).toBe(true);
  const reordered = renderEarlierRun(
    [step("a"), step("b")],
    [step("b"), step("a")],
  );
  expect(reordered.current.earlierVersion).toBe(true);
  const removed = renderEarlierRun([step("a"), step("b")], [step("a")]);
  expect(removed.current.earlierVersion).toBe(true);
});

// An agent made these steps through the API; the editor's first save writes them its own way.
const agentSteps = (extra: Record<string, unknown> = {}) => [
  step("extraction_1", {
    title: "Extract the titles",
    url: null,
    data_extraction_goal: "List the titles",
    data_schema: '{"type":"object","properties":{"titles":{"type":"array"}}}',
    ...extra,
  }),
  step("loop_1", {
    block_type: "for_loop",
    loop_over: {
      parameter_type: "output",
      key: "extraction_1_output",
      output_parameter_id: "op_agent",
    },
    loop_variable_reference:
      "{{ extraction_1_output.extracted_information.titles }}",
    loop_blocks: [step("open_book", { block_type: "navigation", url: "" })],
    complete_if_empty: false,
  }),
];
const editorSaved = (goal = "List the titles") => [
  step("extraction_1", {
    title: "extraction_1",
    url: "",
    data_extraction_goal: goal,
    data_schema: { type: "object", properties: { titles: { type: "array" } } },
  }),
  step("loop_1", {
    block_type: "for_loop",
    loop_over: {
      parameter_type: "output",
      key: "extraction_1_output",
      output_parameter_id: "op_saved",
    },
    loop_variable_reference:
      "{{ extraction_1_output.extracted_information.titles }}",
    loop_blocks: [step("open_book", { block_type: "navigation", url: "" })],
    complete_if_empty: false,
  }),
];

it("keeps every step's result after a save that rewrites fields of steps it did not change", () => {
  const result = renderEarlierRun(agentSteps(), editorSaved());
  expect(result.current.earlierVersion).toBe(false);
  expect(result.current.unknownLabels.size).toBe(0);
});

it("still hides the result of a step whose content the save changed", () => {
  const result = renderEarlierRun(agentSteps(), editorSaved("List the prices"));
  expect(result.current.earlierVersion).toBe(true);
  expect([...result.current.unknownLabels]).toEqual(["extraction_1"]);
});

it("keeps a step's result after a save writes its unpinned 1.0 engine as the workflow's default", () => {
  const result = renderEarlierRun(
    agentSteps({ engine: "skyvern-1.0" }),
    editorSaved(),
    1,
    [[], []],
    { effective_default_engine: null },
  );
  expect(result.current.earlierVersion).toBe(false);
  expect(result.current.unknownLabels.size).toBe(0);
});

it("after a one-step run, the other steps keep the latest full run's results and the step run speaks for its step", async () => {
  urlRun.id = "wr_step";
  localStorage.clear();
  client.get.mockReset();
  client.get.mockResolvedValue({ data: [{ workflow_run_id: "wr_full" }] });
  const row = (label: string, runId: string) => ({
    type: "block",
    block: { workflow_run_id: runId, label, status: "completed" },
    children: [],
  });
  const ran = (blocks: unknown[]) => ({
    status: "completed",
    workflow: { version: 1, workflow_definition: { blocks, parameters: [] } },
  });
  const steps = [
    step("a", { data_extraction_goal: "x" }),
    step("b", { data_extraction_goal: "y" }),
  ];
  timeline.byId = {
    wr_step: [row("b", "wr_step")],
    wr_full: [row("a", "wr_full"), row("b", "wr_full")],
  };
  runQuery.byId = { wr_step: ran(steps), wr_full: ran(steps) };
  const render = (current: unknown[], version = 1, target = "b") => {
    runQuery.current = {
      version,
      workflow_definition: { blocks: current, parameters: [] },
    };
    return renderHook(() => useDocRun(), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={new QueryClient()}>
          <MemoryRouter initialEntries={[`/?bl=${target}`]}>
            <ActiveOrgContext.Provider value="org_a">
              {children}
            </ActiveOrgContext.Provider>
          </MemoryRouter>
        </QueryClientProvider>
      ),
    }).result;
  };
  const data = runQuery.data;
  try {
    const result = render(steps);
    await waitFor(() => expect(result.current.previous?.runId).toBe("wr_full"));
    expect(result.current.blockRun).toBe(true);
    expect(stepShowsEarlierRun(result.current, "a")).toBe(true);
    expect(stepShowsEarlierRun(result.current, "b")).toBe(false);
    expect(result.current.blocksByLabel.get("b")?.workflow_run_id).toBe(
      "wr_step",
    );

    // An edit since the full run leaves that step without a current-looking result.
    const edited = render(
      [
        step("a", { data_extraction_goal: "changed" }),
        step("b", { data_extraction_goal: "y" }),
      ],
      2,
    );
    await waitFor(() => expect(edited.current.previous?.runId).toBe("wr_full"));
    expect(stepShowsEarlierRun(edited.current, "a")).toBe(false);

    // A one-step run of a loop speaks for the steps inside it before they have rows.
    const looped = [
      ...steps,
      step("l", { block_type: "for_loop", loop_blocks: [step("inner")] }),
    ];
    timeline.byId = {
      wr_step: [],
      wr_full: [
        row("a", "wr_full"),
        row("l", "wr_full"),
        row("inner", "wr_full"),
      ],
    };
    runQuery.byId = { wr_step: ran(looped), wr_full: ran(looped) };
    const loop = render(looped, 1, "l");
    await waitFor(() => expect(loop.current.previous?.runId).toBe("wr_full"));
    expect(stepShowsEarlierRun(loop.current, "a")).toBe(true);
    expect(stepShowsEarlierRun(loop.current, "inner")).toBe(false);

    // Until the one-step run's own definition loads, the current one keeps the loop's steps in its scope.
    runQuery.byId = { wr_full: ran(looped) };
    runQuery.data = undefined;
    const unloaded = render(looped, 1, "l");
    await waitFor(() =>
      expect(unloaded.current.previous?.runId).toBe("wr_full"),
    );
    expect(unloaded.current.run).toBeUndefined();
    expect(stepShowsEarlierRun(unloaded.current, "a")).toBe(true);
    expect(stepShowsEarlierRun(unloaded.current, "inner")).toBe(false);
  } finally {
    runQuery.data = data;
    timeline.byId = {};
    runQuery.byId = {};
    urlRun.id = "wr_1";
  }
});

it("after a one-step run inside an If or a loop, the containers read that run and the If opens the step's branch", async () => {
  urlRun.id = "wr_step";
  localStorage.clear();
  client.get.mockReset();
  client.get.mockResolvedValue({ data: [{ workflow_run_id: "wr_full" }] });
  const row = (label: string, runId: string, extra = {}) => ({
    type: "block",
    block: { workflow_run_id: runId, label, status: "completed", ...extra },
    children: [],
  });
  const extraction = (label: string, next: string | null) =>
    block("extraction", label, next, { data_extraction_goal: label });
  const blocks = () => [
    extraction("before", "check"),
    conditional("check", "each", [
      { id: "b_b", next: "in_b", when: "{{ x }}", exact: true },
      { id: "b_a", next: "in_a" },
    ]),
    extraction("in_b", "each"),
    extraction("in_a", "each"),
    forLoop("each", null, [extraction("inner", null)]),
  ];
  const definition = () => ({ blocks: blocks(), parameters: [] });
  timeline.byId = {
    wr_full: [
      row("before", "wr_full"),
      row("check", "wr_full", { executed_branch_id: "b_b" }),
      row("in_b", "wr_full"),
      row("each", "wr_full"),
      row("inner", "wr_full"),
    ],
  };
  runQuery.byId = {
    wr_step: {
      status: "completed",
      workflow: { version: 1, workflow_definition: definition() },
    },
    wr_full: {
      status: "completed",
      workflow: { version: 1, workflow_definition: definition() },
    },
  };
  runQuery.current = { version: 1, workflow_definition: definition() };
  const render = (target: string) =>
    renderHook(() => useDocRun(), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={new QueryClient()}>
          <MemoryRouter initialEntries={[`/?bl=${target}`]}>
            <ActiveOrgContext.Provider value="org_a">
              {children}
            </ActiveOrgContext.Provider>
          </MemoryRouter>
        </QueryClientProvider>
      ),
    }).result;
  try {
    timeline.byId.wr_step = [row("in_a", "wr_step")];
    const branch = render("in_a");
    await waitFor(() => expect(branch.current.previous?.runId).toBe("wr_full"));
    const graph = getElements(blocks(), settings(), true);
    const item = buildDocTree(graph.nodes, graph.edges).items.find(
      (i) => i.kind === "conditional",
    )!;
    const { selected } = docBranchSelection(
      item as Extract<typeof item, { kind: "conditional" }>,
      "check",
      undefined,
      new Map(graph.nodes.map((node) => [node.id, node])),
      docRunFor(branch.current, "check").blocksByLabel,
    );
    expect(selected.branchId).toBe("b_a");
    expect(
      docRunFor(branch.current, "in_a").blocksByLabel.get("in_a")
        ?.workflow_run_id,
    ).toBe("wr_step");
    expect(docRunFor(branch.current, "before")).toMatchObject({ older: true });
    expect(
      docRunFor(branch.current, "before").blocksByLabel.get("before")
        ?.workflow_run_id,
    ).toBe("wr_full");

    timeline.byId.wr_step = [row("inner", "wr_step")];
    const looped = render("inner");
    await waitFor(() => expect(looped.current.previous?.runId).toBe("wr_full"));
    expect(docRunFor(looped.current, "each")).toMatchObject({ older: false });
    expect(docRunFor(looped.current, "each").blocksByLabel.has("each")).toBe(
      false,
    );
    expect(docRunFor(looped.current, "before")).toMatchObject({ older: true });
  } finally {
    timeline.byId = {};
    runQuery.byId = {};
    urlRun.id = "wr_1";
  }
});

it("treats a run of the current version as current", () => {
  const result = renderEarlierRun(
    [step("a")],
    [step("a", { data_extraction_goal: "new" })],
    2,
  );
  expect(result.current.earlierVersion).toBe(false);
  expect(result.current.unknownLabels.size).toBe(0);
});

it("shows the agent's latest run to a browser that never started it, until this browser clears it", async () => {
  urlRun.id = undefined;
  localStorage.clear();
  client.get.mockResolvedValue({ data: [{ workflow_run_id: "wr_other" }] });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <ActiveOrgContext.Provider value="org_a">
          {children}
        </ActiveOrgContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>
  );
  const { result } = renderHook(() => useDocRun(), { wrapper });
  await waitFor(() => expect(result.current.runId).toBe("wr_other"));

  useDocRunStore.getState().clearDocRun("org_a", "wpid_1", "wr_other");
  await waitFor(() => expect(result.current.runId).toBeUndefined());

  client.get.mockResolvedValue({ data: [{ workflow_run_id: "wr_newer" }] });
  await result.current.runId;
  const fresh = renderHook(() => useDocRun(), { wrapper });
  await waitFor(() => expect(fresh.result.current.runId).toBe("wr_newer"));
  urlRun.id = "wr_1";
});

it("picks up a run another browser starts while the Doc stays open, on focus and on the poll", async () => {
  urlRun.id = undefined;
  localStorage.clear();
  vi.useFakeTimers({ shouldAdvanceTime: true });
  client.get.mockResolvedValue({ data: [{ workflow_run_id: "wr_first" }] });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <ActiveOrgContext.Provider value="org_a">
          {children}
        </ActiveOrgContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>
  );
  const { result } = renderHook(() => useDocRun(), { wrapper });
  await waitFor(() => expect(result.current.runId).toBe("wr_first"));

  client.get.mockResolvedValue({ data: [{ workflow_run_id: "wr_second" }] });
  act(() => {
    focusManager.setFocused(false);
    focusManager.setFocused(true);
  });
  await waitFor(() => expect(result.current.runId).toBe("wr_second"));

  try {
    client.get.mockResolvedValue({ data: [{ workflow_run_id: "wr_third" }] });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    await waitFor(() => expect(result.current.runId).toBe("wr_third"));
  } finally {
    vi.useRealTimers();
    urlRun.id = "wr_1";
  }
});

it("clears the shared run, not the ?wr= run on screen, so Clear leaves nothing behind", async () => {
  urlRun.id = "wr_old";
  localStorage.clear();
  client.get.mockResolvedValue({ data: [{ workflow_run_id: "wr_latest" }] });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <ActiveOrgContext.Provider value="org_a">
          {children}
        </ActiveOrgContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>
  );
  const { result, rerender } = renderHook(() => useDocRun(), { wrapper });
  await waitFor(() => expect(result.current.sharedRunId).toBe("wr_latest"));
  expect(result.current.runId).toBe("wr_old");

  useDocRunStore
    .getState()
    .clearDocRun("org_a", "wpid_1", result.current.sharedRunId ?? undefined);
  urlRun.id = undefined;
  rerender();
  await waitFor(() => expect(result.current.runId).toBeUndefined());
  urlRun.id = "wr_1";
});

const sharedWrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={new QueryClient()}>
    <MemoryRouter>
      <ActiveOrgContext.Provider value="org_a">
        {children}
      </ActiveOrgContext.Provider>
    </MemoryRouter>
  </QueryClientProvider>
);

it("draws a run from elsewhere as running but never owns it, so it cannot lock the Doc or get a Stop", async () => {
  urlRun.id = undefined;
  localStorage.clear();
  runQuery.data = { status: "running", workflow: { version: 1 } };
  client.get.mockResolvedValue({ data: [{ workflow_run_id: "wr_teammate" }] });
  const { result } = renderHook(() => useDocRun(), { wrapper: sharedWrapper });
  await waitFor(() => expect(result.current.runId).toBe("wr_teammate"));
  expect(result.current.live).toBe(true);
  expect(result.current.owned).toBe(false);

  useDocRunStore.getState().setDocRunId("org_a", "wpid_1", "wr_teammate");
  await waitFor(() => expect(result.current.owned).toBe(true));
  runQuery.data = undefined;
  urlRun.id = "wr_1";
});

it("keeps this browser's own active run on screen when a newer run starts elsewhere, until it ends", async () => {
  urlRun.id = undefined;
  localStorage.clear();
  runQuery.byId = {
    wr_mine: { status: "running", workflow: { version: 1 } },
  };
  useDocRunStore.getState().setDocRunId("org_a", "wpid_1", "wr_mine");
  client.get.mockResolvedValue({ data: [{ workflow_run_id: "wr_newer" }] });
  const { result, rerender } = renderHook(() => useDocRun(), {
    wrapper: sharedWrapper,
  });
  await waitFor(() => expect(client.get).toHaveBeenCalled());
  expect(result.current.runId).toBe("wr_mine");
  expect(result.current.owned).toBe(true);

  runQuery.byId = {
    wr_mine: { status: "completed", workflow: { version: 1 } },
  };
  rerender();
  await waitFor(() => expect(result.current.runId).toBe("wr_newer"));
  runQuery.byId = {};
  urlRun.id = "wr_1";
});

it("skips a step run started in this browser when choosing the latest run", async () => {
  urlRun.id = undefined;
  localStorage.clear();
  markStepRun("wr_step");
  client.get.mockResolvedValue({
    data: [{ workflow_run_id: "wr_step" }, { workflow_run_id: "wr_full" }],
  });
  const { result } = renderHook(() => useDocRun(), { wrapper: sharedWrapper });
  await waitFor(() => expect(result.current.runId).toBe("wr_full"));
  urlRun.id = "wr_1";
});

it("skips a one-step run the server marks as a debug-session run, even from a browser that did not start it", async () => {
  urlRun.id = undefined;
  localStorage.clear();
  client.get.mockResolvedValue({
    data: [
      { workflow_run_id: "wr_step", debug_session_id: "ds_1" },
      { workflow_run_id: "wr_full", debug_session_id: null },
    ],
  });
  const { result } = renderHook(() => useDocRun(), { wrapper: sharedWrapper });
  await waitFor(() => expect(result.current.runId).toBe("wr_full"));
  urlRun.id = "wr_1";
});

it("pages back past one-step runs once, then later polls read the first page only", async () => {
  urlRun.id = undefined;
  localStorage.clear();
  const page = (n: number, full?: string) =>
    Array.from({ length: 20 }, (_, i) => ({
      workflow_run_id: i === 19 && full ? full : `wr_step_${n}_${i}`,
      debug_session_id: i === 19 && full ? null : "ds_1",
    }));
  client.get.mockReset();
  let firstPage = page(1);
  client.get.mockImplementation(
    async (_url: string, config?: { params?: { page?: number } }) => ({
      data:
        config?.params?.page === 1
          ? firstPage
          : config?.params?.page === 2
            ? page(2, "wr_full")
            : page(config?.params?.page ?? 0),
    }),
  );
  const queryClient = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ActiveOrgContext.Provider value="org_a">
          {children}
        </ActiveOrgContext.Provider>
      </MemoryRouter>
    </QueryClientProvider>
  );
  const { result } = renderHook(
    () => {
      // Every step calls the hook; they share one query and one answer.
      useDocRun();
      return useDocRun();
    },
    { wrapper },
  );
  await waitFor(() => expect(result.current.runId).toBe("wr_full"));
  expect(client.get).toHaveBeenCalledTimes(2);

  // Later polls: one more request each, and the full run found earlier stays.
  firstPage = page(3);
  await act(() =>
    queryClient.invalidateQueries({
      queryKey: docLatestRunKey("org_a", "wpid_1"),
    }),
  );
  await waitFor(() => expect(client.get).toHaveBeenCalledTimes(3));
  expect(result.current.runId).toBe("wr_full");

  // A new full run on the first page replaces it.
  firstPage = [
    { workflow_run_id: "wr_newer", debug_session_id: null },
  ] as never;
  await act(() =>
    queryClient.invalidateQueries({
      queryKey: docLatestRunKey("org_a", "wpid_1"),
    }),
  );
  await waitFor(() => expect(result.current.runId).toBe("wr_newer"));
  expect(client.get).toHaveBeenCalledTimes(4);
  client.get.mockReset();
  urlRun.id = "wr_1";
});

it("still recognises a step run when storage is blocked", () => {
  const blocked = vi
    .spyOn(Storage.prototype, "setItem")
    .mockImplementation(() => {
      throw new Error("blocked");
    });
  markStepRun("wr_blocked");
  blocked.mockRestore();
  expect(isStepRun("wr_blocked")).toBe(true);
});
