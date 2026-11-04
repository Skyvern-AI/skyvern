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
import { markStepRun, useDocRunStore } from "./docRunStore";
import { useDocRun } from "./useDocRun";

const timeline = vi.hoisted(() => ({
  data: [] as WorkflowRunTimelineItem[],
  isPlaceholderData: false,
}));
const urlRun = vi.hoisted(() => ({ id: "wr_1" as string | undefined }));
vi.mock("../../studio/useStudioRunId", () => ({
  useStudioRunId: () => urlRun.id,
}));
vi.mock("../../hooks/useWorkflowRunTimelineQuery", () => ({
  useWorkflowRunTimelineQuery: () => timeline,
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
) {
  timeline.data = [failedRow("a"), failedRow("b")];
  timeline.isPlaceholderData = false;
  runQuery.data = {
    workflow: {
      version: runVersion,
      workflow_definition: { blocks: runBlocks },
    },
  };
  runQuery.current = {
    version: 2,
    workflow_definition: { blocks: currentBlocks },
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
    [step("a", { goal: "x" }), step("b", { goal: "old" })],
    [step("a", { goal: "x" }), step("b", { goal: "new" }), step("c")],
  );
  expect(result.current.earlierVersion).toBe(true);
  expect([...result.current.unknownLabels].sort()).toEqual(["b", "c"]);
  expect(result.current.blocksByLabel.has("a")).toBe(true);
});

it("treats a run of the current version as current", () => {
  const result = renderEarlierRun([step("a")], [step("a", { goal: "new" })], 2);
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
