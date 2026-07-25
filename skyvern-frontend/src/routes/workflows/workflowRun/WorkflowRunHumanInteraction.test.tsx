// @vitest-environment jsdom

vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

const useWorkflowRunWithWorkflowQueryMock = vi.fn();
vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: (options: unknown) =>
    useWorkflowRunWithWorkflowQueryMock(options),
}));

import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
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

function renderInteraction(block: WorkflowRunBlock) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <WorkflowRunHumanInteraction workflowRunBlock={block} />
    </QueryClientProvider>,
  );
}

describe("WorkflowRunHumanInteraction", () => {
  beforeEach(() => {
    useWorkflowRunWithWorkflowQueryMock.mockClear();
    useWorkflowRunWithWorkflowQueryMock.mockReturnValue({
      data: { workflow_run_id: "wr_1", status: Status.Paused },
    });
  });
  afterEach(cleanup);

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
    useWorkflowRunWithWorkflowQueryMock.mockReturnValue({
      data: { workflow_run_id: "wr_studio", status: Status.Paused },
    });
    renderInteraction(
      buildBlock({ workflow_run_id: "wr_studio", status: Status.Running }),
    );
    expect(useWorkflowRunWithWorkflowQueryMock).toHaveBeenCalledWith({
      workflowRunId: "wr_studio",
    });
    expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy();
  });

  it("renders nothing when the resolved run is not this block's run", () => {
    // keepPreviousData can briefly surface the prior (paused) run while switching;
    // the buttons must not act on a run that isn't this block's.
    useWorkflowRunWithWorkflowQueryMock.mockReturnValue({
      data: { workflow_run_id: "wr_other", status: Status.Paused },
    });
    const { container } = renderInteraction(
      buildBlock({ workflow_run_id: "wr_1", status: Status.Running }),
    );
    expect(container.textContent).toBe("");
  });

  it("shows a default message when instructions are empty", () => {
    renderInteraction(buildBlock({ instructions: null }));
    expect(
      screen.getByText("The agent is paused and waiting for your review."),
    ).toBeTruthy();
  });

  it("renders nothing when the run is not paused", () => {
    useWorkflowRunWithWorkflowQueryMock.mockReturnValue({
      data: { workflow_run_id: "wr_1", status: Status.Running },
    });
    const { container } = renderInteraction(buildBlock());
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
