// @vitest-environment jsdom

const { getClientMock, realRunQuery } = vi.hoisted(() => ({
  getClientMock: vi.fn(),
  // The identity case drives the real query through a seeded cache; the rest
  // only need a payload, so they keep the cheaper stub.
  realRunQuery: { enabled: false },
}));

vi.mock("@/api/AxiosClient", () => ({ getClient: getClientMock }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

const runQueryStub = vi.fn();
vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", async (importOriginal) => {
  const actual =
    await importOriginal<
      typeof import("../hooks/useWorkflowRunWithWorkflowQuery")
    >();
  return {
    useWorkflowRunWithWorkflowQuery: (
      options?: Parameters<typeof actual.useWorkflowRunWithWorkflowQuery>[0],
    ) =>
      realRunQuery.enabled
        ? actual.useWorkflowRunWithWorkflowQuery(options)
        : runQueryStub(options),
  };
});

import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Status } from "@/api/types";
import type { WorkflowRunBlock } from "../types/workflowRunTypes";
import { WorkflowRunHumanInteraction } from "./WorkflowRunHumanInteraction";

function buildBlock(
  overrides: Partial<WorkflowRunBlock> = {},
): WorkflowRunBlock {
  return {
    workflow_run_block_id: "wrb_1",
    workflow_run_id: "wr_1",
    block_type: "human_interaction",
    // running = this block is the one currently awaiting interaction
    status: Status.Running,
    instructions: null,
    positive_descriptor: null,
    negative_descriptor: null,
    ...overrides,
  } as unknown as WorkflowRunBlock;
}

function renderInteraction(
  block: WorkflowRunBlock,
  client = new QueryClient(),
) {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/runs/wr_1"]}>
        <WorkflowRunHumanInteraction workflowRunBlock={block} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("WorkflowRunHumanInteraction", () => {
  beforeEach(() => {
    runQueryStub.mockClear();
    runQueryStub.mockReturnValue({
      data: { workflow_run_id: "wr_1", status: Status.Paused },
    });
  });
  afterEach(() => {
    cleanup();
    realRunQuery.enabled = false;
    getClientMock.mockReset();
  });

  it("falls back to Approve/Reject when descriptors are empty", () => {
    renderInteraction(
      buildBlock({ positive_descriptor: null, negative_descriptor: "" }),
    );
    expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
  });

  it("uses custom descriptors when present", () => {
    renderInteraction(
      buildBlock({ positive_descriptor: "Yes", negative_descriptor: "No" }),
    );
    expect(screen.getByRole("button", { name: "Yes" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "No" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it("resolves the run from the block id, not a route param", () => {
    runQueryStub.mockReturnValue({
      data: { workflow_run_id: "wr_studio", status: Status.Paused },
    });
    renderInteraction(
      buildBlock({ workflow_run_id: "wr_studio", status: Status.Running }),
    );
    expect(runQueryStub).toHaveBeenCalledWith({
      workflowRunId: "wr_studio",
    });
    expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy();
  });

  it("shows a default message when instructions are empty", () => {
    renderInteraction(buildBlock({ instructions: null }));
    expect(
      screen.getByText("The agent is paused and waiting for your review."),
    ).toBeTruthy();
  });

  it("renders nothing when the run is not paused", () => {
    runQueryStub.mockReturnValue({
      data: { workflow_run_id: "wr_1", status: Status.Running },
    });
    const { container } = renderInteraction(buildBlock());
    expect(container.textContent).toBe("");
  });

  it("renders nothing when the resolved run is not this block's run", () => {
    realRunQuery.enabled = true;
    getClientMock.mockResolvedValue({ get: () => new Promise(() => {}) });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    client.setQueryData(["workflowRun", "wr_1"], {
      workflow_run_id: "wr_other",
      status: Status.Paused,
    });

    const { container } = renderInteraction(
      buildBlock({ workflow_run_id: "wr_1", status: Status.Running }),
      client,
    );

    expect(container.textContent).toBe("");
  });

  it("renders nothing for a resolved block while the run is paused elsewhere", () => {
    // Run paused at a later HITL block, but THIS block already resolved (completed).
    // Its buttons must NOT show, or a stale prompt would cancel the wrong pause.
    const { container } = renderInteraction(
      buildBlock({ status: Status.Completed }),
    );
    expect(container.textContent).toBe("");
  });
});
