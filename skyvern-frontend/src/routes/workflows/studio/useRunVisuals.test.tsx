// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { type ReactNode } from "react";

import { Status } from "@/api/types";
import { useRunViewStore } from "@/store/RunViewStore";
import type {
  WorkflowRunBlock,
  WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";
import { useRunVisuals } from "./useRunVisuals";

const { mocks, getClientMock } = vi.hoisted(() => ({
  mocks: {
    workflowRun: undefined as unknown,
    timeline: undefined as unknown,
    // The identity cases below need the real queries; the projection cases above
    // them only need a payload, so they keep the cheaper stub.
    useRealQueries: false,
  },
  getClientMock: vi.fn(),
}));

vi.mock("@/api/AxiosClient", () => ({ getClient: getClientMock }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => undefined,
}));
vi.mock("../hooks/useGlobalWorkflowsQuery", () => ({
  useGlobalWorkflowsQuery: () => ({ data: [] }),
}));
vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", async (importOriginal) => {
  const actual =
    await importOriginal<
      typeof import("../hooks/useWorkflowRunWithWorkflowQuery")
    >();
  return {
    useWorkflowRunWithWorkflowQuery: (
      options?: Parameters<typeof actual.useWorkflowRunWithWorkflowQuery>[0],
    ) =>
      mocks.useRealQueries
        ? actual.useWorkflowRunWithWorkflowQuery(options)
        : { data: mocks.workflowRun, isLoading: false },
  };
});
vi.mock("../hooks/useWorkflowRunTimelineQuery", async (importOriginal) => {
  const actual =
    await importOriginal<
      typeof import("../hooks/useWorkflowRunTimelineQuery")
    >();
  return {
    useWorkflowRunTimelineQuery: (
      options?: Parameters<typeof actual.useWorkflowRunTimelineQuery>[0],
    ) =>
      mocks.useRealQueries
        ? actual.useWorkflowRunTimelineQuery(options)
        : { data: mocks.timeline, isLoading: false },
  };
});

function buildBlock(
  overrides: Partial<WorkflowRunBlock> = {},
): WorkflowRunBlock {
  return {
    workflow_run_block_id: "wrb_default",
    workflow_run_id: "wr_default",
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
    ...overrides,
  };
}

function buildBlockItem(
  block: WorkflowRunBlock,
  children: Array<WorkflowRunTimelineItem> = [],
): WorkflowRunTimelineItem {
  return {
    type: "block",
    block,
    children,
    thought: null,
    created_at: block.created_at,
    modified_at: block.modified_at,
  };
}

function seedLoopRun() {
  const loop = buildBlock({
    workflow_run_block_id: "wrb_loop",
    block_type: "for_loop",
    label: "checkout-loop",
    loop_values: ["alpha", "beta"],
    current_index: 0,
    created_at: "2026-06-10T00:00:00Z",
  });
  const iter0 = buildBlock({
    workflow_run_block_id: "wrb_iter0",
    parent_workflow_run_block_id: "wrb_loop",
    current_index: 0,
    created_at: "2026-06-10T00:00:10Z",
  });
  const iter1 = buildBlock({
    workflow_run_block_id: "wrb_iter1",
    parent_workflow_run_block_id: "wrb_loop",
    current_index: 1,
    created_at: "2026-06-10T00:00:20Z",
  });
  mocks.timeline = [
    buildBlockItem(loop, [buildBlockItem(iter0), buildBlockItem(iter1)]),
  ];
  mocks.workflowRun = {
    workflow_run_id: "wr_1",
    status: Status.Completed,
    workflow: {
      workflow_definition: { blocks: [], finally_block_label: null },
    },
  };
}

function wrapper({ children }: { children: ReactNode }) {
  return (
    <MemoryRouter initialEntries={["/?active=wrb_loop"]}>
      {children}
    </MemoryRouter>
  );
}

afterEach(() => {
  mocks.workflowRun = undefined;
  mocks.timeline = undefined;
  mocks.useRealQueries = false;
  getClientMock.mockReset();
});
beforeEach(() => useRunViewStore.getState().reset());

describe("useRunVisuals loop-iteration threading", () => {
  test("resolves a selected container without an iteration to its first leaf", () => {
    seedLoopRun();
    const { result } = renderHook(() => useRunVisuals("wr_1"), { wrapper });

    expect(result.current.heroSelection).toMatchObject({
      kind: "block",
      workflowRunBlockId: "wrb_iter0",
    });
  });

  test("resolves the Overview pane's selected iteration from the shared store", () => {
    seedLoopRun();
    useRunViewStore.getState().pinFrame("wrb_loop", 1);
    const { result } = renderHook(() => useRunVisuals("wr_1"), { wrapper });

    expect(result.current.heroSelection).toMatchObject({
      kind: "block",
      workflowRunBlockId: "wrb_iter1",
    });
  });
});

const RUN_A_ID = "wr_visuals_a";
const RUN_B_ID = "wr_visuals_b";

function realQueryWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/agents/wpid_1/studio"]}>
          {children}
        </MemoryRouter>
      </QueryClientProvider>
    );
  };
}

describe("useRunVisuals identity", () => {
  test("reports no timeline, filmstrip or hero for a run other than the one requested", async () => {
    mocks.useRealQueries = true;
    const runA = {
      workflow_run_id: RUN_A_ID,
      status: Status.Running,
      workflow: {
        workflow_permanent_id: "wpid_1",
        workflow_definition: { blocks: [], finally_block_label: null },
      },
    };
    const timelineA = [
      buildBlockItem(
        buildBlock({
          workflow_run_block_id: "wrb_run_a",
          workflow_run_id: RUN_A_ID,
          label: "run-a-block",
        }),
      ),
    ];
    getClientMock.mockResolvedValue({
      get: (url: string) => {
        if (!url.includes(RUN_A_ID)) {
          return new Promise(() => {});
        }
        return Promise.resolve({
          data: url.endsWith("/timeline") ? timelineA : runA,
        });
      },
    });

    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const { result, rerender } = renderHook(
      ({ id }: { id: string | undefined }) => useRunVisuals(id),
      {
        wrapper: realQueryWrapper(client),
        initialProps: { id: RUN_A_ID as string | undefined },
      },
    );

    await waitFor(() => {
      expect(result.current.workflowRun?.workflow_run_id).toBe(RUN_A_ID);
      expect(result.current.timeline).toHaveLength(1);
    });

    rerender({ id: RUN_B_ID });

    expect(result.current.workflowRun).toBeUndefined();
    expect(result.current.timeline).toBeUndefined();
    expect(result.current.heroSelection).toBeNull();
    expect(result.current.hasScreenshots).toBe(false);

    rerender({ id: undefined });

    expect(result.current.workflowRun).toBeUndefined();
    expect(result.current.timeline).toBeUndefined();
    expect(result.current.heroSelection).toBeNull();
  });
});
