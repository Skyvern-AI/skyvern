// @vitest-environment jsdom

import { renderHook } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Status } from "@/api/types";

const workflowRunQueryMock = vi.hoisted(() => vi.fn());

vi.mock("../hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: (options?: { workflowRunId?: string }) =>
    workflowRunQueryMock(options),
}));

import { useRunLifecycleAnnouncements } from "./useRunLifecycleAnnouncements";

type RunFixture = {
  workflow_run_id: string;
  status: Status;
  created_at: string;
  started_at: string | null;
  finished_at: string;
  outputs: Record<string, unknown> | null;
  failure_reason: string | null;
};

function makeRun(overrides: Partial<RunFixture> = {}): RunFixture {
  return {
    workflow_run_id: "wr_1",
    status: Status.Running,
    created_at: "2026-01-01T00:00:00Z",
    started_at: null,
    finished_at: "2026-01-01T00:00:10Z",
    outputs: null,
    failure_reason: null,
    ...overrides,
  };
}

function renderLifecycle(initial: {
  workflowRunId: string | undefined;
  inFlight?: boolean;
  search?: string;
}) {
  const turnInFlightRef = { current: initial.inFlight ?? false };
  const announce = vi.fn();
  const view = renderHook(
    ({ workflowRunId }: { workflowRunId: string | undefined }) =>
      useRunLifecycleAnnouncements({
        workflowRunId,
        turnInFlightRef,
        announce,
      }),
    {
      initialProps: { workflowRunId: initial.workflowRunId },
      wrapper: ({ children }) => (
        <MemoryRouter initialEntries={[`/?${initial.search ?? ""}`]}>
          {children}
        </MemoryRouter>
      ),
    },
  );
  return { ...view, announce, turnInFlightRef };
}

beforeEach(() => {
  workflowRunQueryMock.mockReset();
});

describe("useRunLifecycleAnnouncements", () => {
  it("announces start once on first non-finalized observation; a same-data rerender does not double-announce", () => {
    workflowRunQueryMock.mockReturnValue({ data: makeRun() });
    const { announce, rerender } = renderLifecycle({ workflowRunId: "wr_1" });

    expect(announce).toHaveBeenCalledTimes(1);
    expect(announce.mock.calls[0]![0]).toMatchObject({
      id: "run-lifecycle-wr_1-start",
      kind: "run_lifecycle",
      content: "Run started — watching it now.",
    });

    rerender({ workflowRunId: "wr_1" });
    expect(announce).toHaveBeenCalledTimes(1);
  });

  it("announces a terminal message with duration and extracted count on running -> completed", () => {
    workflowRunQueryMock.mockReturnValue({ data: makeRun() });
    const { announce, rerender } = renderLifecycle({ workflowRunId: "wr_1" });
    expect(announce).toHaveBeenCalledTimes(1);

    workflowRunQueryMock.mockReturnValue({
      data: makeRun({
        status: Status.Completed,
        started_at: "2026-01-01T00:00:00Z",
        finished_at: "2026-01-01T00:01:05Z",
        outputs: { extracted_information: [1, 2, 3, 4, 5] },
      }),
    });
    rerender({ workflowRunId: "wr_1" });

    expect(announce).toHaveBeenCalledTimes(2);
    expect(announce.mock.calls[1]![0]).toMatchObject({
      id: "run-lifecycle-wr_1-terminal",
      content:
        "Run completed in 1:05 — extracted 5 item(s). Want to review or change anything?",
    });
  });

  it("counts a single named array wrapping the extracted output (object-shaped extracted_information)", () => {
    workflowRunQueryMock.mockReturnValue({ data: makeRun() });
    const { announce, rerender } = renderLifecycle({ workflowRunId: "wr_1" });
    expect(announce).toHaveBeenCalledTimes(1);

    workflowRunQueryMock.mockReturnValue({
      data: makeRun({
        status: Status.Completed,
        outputs: { extracted_information: { rows: [1, 2, 3] } },
      }),
    });
    rerender({ workflowRunId: "wr_1" });

    expect(announce.mock.calls[1]![0].content).toContain("extracted 3 item(s)");
  });

  it("omits the count when extracted_information has zero or multiple array-valued keys", () => {
    workflowRunQueryMock.mockReturnValue({ data: makeRun() });
    const { announce, rerender } = renderLifecycle({ workflowRunId: "wr_1" });
    expect(announce).toHaveBeenCalledTimes(1);

    workflowRunQueryMock.mockReturnValue({
      data: makeRun({
        status: Status.Completed,
        outputs: { extracted_information: { a: [1], b: [2] } },
      }),
    });
    rerender({ workflowRunId: "wr_1" });

    expect(announce.mock.calls[1]![0].content).toBe(
      "Run completed in 0:10. Want to review or change anything?",
    );
  });

  it("stays permanently silent when a run is first observed already finalized", () => {
    workflowRunQueryMock.mockReturnValue({
      data: makeRun({ status: Status.Completed }),
    });
    const { announce, rerender } = renderLifecycle({ workflowRunId: "wr_1" });
    expect(announce).not.toHaveBeenCalled();

    rerender({ workflowRunId: "wr_1" });
    expect(announce).not.toHaveBeenCalled();
  });

  it("announces a failed-run terminal message with a truncated reason", () => {
    workflowRunQueryMock.mockReturnValue({ data: makeRun() });
    const { announce, rerender } = renderLifecycle({ workflowRunId: "wr_1" });
    expect(announce).toHaveBeenCalledTimes(1);

    const longReason = "x".repeat(250);
    workflowRunQueryMock.mockReturnValue({
      data: makeRun({
        status: Status.Failed,
        started_at: "2026-01-01T00:00:00Z",
        finished_at: "2026-01-01T00:00:30Z",
        failure_reason: longReason,
      }),
    });
    rerender({ workflowRunId: "wr_1" });

    expect(announce).toHaveBeenCalledTimes(2);
    const message = announce.mock.calls[1]![0].content as string;
    expect(message.startsWith("Run failed after 0:30 — ")).toBe(true);
    expect(message).toContain(longReason.slice(0, 200));
    expect(message).not.toContain(longReason.slice(0, 201));
    expect(message.endsWith("Ask me to diagnose and fix it.")).toBe(true);
  });

  it("announces a canceled-run terminal message", () => {
    workflowRunQueryMock.mockReturnValue({ data: makeRun() });
    const { announce, rerender } = renderLifecycle({ workflowRunId: "wr_1" });
    expect(announce).toHaveBeenCalledTimes(1);

    workflowRunQueryMock.mockReturnValue({
      data: makeRun({ status: Status.Canceled }),
    });
    rerender({ workflowRunId: "wr_1" });

    expect(announce).toHaveBeenCalledTimes(2);
    expect(announce.mock.calls[1]![0].content).toBe("Run canceled.");
  });

  it("ignores a stale placeholder payload still keyed to the previous run id (keepPreviousData trap)", () => {
    // wr_a observed already-finalized on first sight: permanently silent.
    workflowRunQueryMock.mockReturnValue({
      data: makeRun({ workflow_run_id: "wr_a", status: Status.Completed }),
    });
    const { announce, rerender } = renderLifecycle({ workflowRunId: "wr_a" });
    expect(announce).not.toHaveBeenCalled();

    // Studio focus moves to wr_b, but keepPreviousData is still serving wr_a's
    // payload as a placeholder while the new query is in flight — the hook
    // input changed, the payload's own workflow_run_id did not.
    rerender({ workflowRunId: "wr_b" });
    expect(announce).not.toHaveBeenCalled();
  });

  it("stays permanently silent for a run first observed while a copilot turn is in flight", () => {
    workflowRunQueryMock.mockReturnValue({ data: makeRun() });
    const { announce, rerender } = renderLifecycle({
      workflowRunId: "wr_1",
      inFlight: true,
    });
    expect(announce).not.toHaveBeenCalled();

    workflowRunQueryMock.mockReturnValue({
      data: makeRun({ status: Status.Completed }),
    });
    rerender({ workflowRunId: "wr_1" });
    expect(announce).not.toHaveBeenCalled();
  });

  it("does nothing when workflowRunId is undefined, and disables the query instead of letting it fall back to the route", () => {
    workflowRunQueryMock.mockReturnValue({ data: undefined });
    const { announce } = renderLifecycle({ workflowRunId: undefined });

    expect(announce).not.toHaveBeenCalled();
    expect(workflowRunQueryMock).toHaveBeenCalledWith({
      workflowRunId: undefined,
      enabled: false,
    });
  });

  it("uses the joined copy when started_at is more than 15s old", () => {
    workflowRunQueryMock.mockReturnValue({
      data: makeRun({
        started_at: new Date(Date.now() - 20_000).toISOString(),
      }),
    });
    const { announce } = renderLifecycle({ workflowRunId: "wr_1" });

    expect(announce).toHaveBeenCalledTimes(1);
    expect(announce.mock.calls[0]![0].content).toBe(
      "Run in progress — watching it now.",
    );
  });

  it("uses the block-run copy variant when ?bl= is present", () => {
    workflowRunQueryMock.mockReturnValue({ data: makeRun() });
    const { announce } = renderLifecycle({
      workflowRunId: "wr_1",
      search: "bl=block_1",
    });

    expect(announce).toHaveBeenCalledTimes(1);
    expect(announce.mock.calls[0]![0].content).toBe(
      "Block run started — watching it now.",
    );
  });
});
