// @vitest-environment jsdom
import type { ReactNode } from "react";
import { renderHook } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ActiveOrgContext } from "@/store/ActiveOrgContext";
import type { WorkflowRunTimelineItem } from "../../types/workflowRunTypes";
import { useDocRun } from "./useDocRun";

const timeline = vi.hoisted(() => ({
  data: [] as WorkflowRunTimelineItem[],
  isPlaceholderData: false,
}));
vi.mock("../../studio/useStudioRunId", () => ({
  useStudioRunId: () => "wr_1",
}));
vi.mock("../../hooks/useWorkflowRunTimelineQuery", () => ({
  useWorkflowRunTimelineQuery: () => timeline,
}));
vi.mock("../../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: () => ({ data: undefined }),
}));

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
      <ActiveOrgContext.Provider value={orgId}>
        {children}
      </ActiveOrgContext.Provider>
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
