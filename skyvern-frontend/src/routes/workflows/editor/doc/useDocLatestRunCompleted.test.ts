import { renderHook } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { Status } from "@/api/types";
import { useDocLatestRunCompleted } from "./useDocLatestRunCompleted";

const state = vi.hoisted(() => ({
  run: undefined as Record<string, unknown> | undefined,
}));
vi.mock("../../hooks/useWorkflowRunsQuery", () => ({
  useWorkflowRunsQuery: () => ({ data: state.run ? [state.run] : [] }),
}));
vi.mock("../../hooks/useWorkflowQuery", () => ({
  useWorkflowQuery: () => ({ data: { workflow_id: "w_v2" } }),
}));

const ran = (run: Record<string, unknown>) => {
  state.run = { status: Status.Completed, workflow_id: "w_v2", ...run };
  return renderHook(() => useDocLatestRunCompleted("wpid_1")).result.current;
};

describe("useDocLatestRunCompleted", () => {
  test("counts only a completed full run of the saved version", () => {
    expect(ran({})).toBe(true);
    expect(ran({ status: Status.Failed })).toBe(false);
    expect(ran({ debug_session_id: "ds_1" })).toBe(false);
    expect(ran({ workflow_id: "w_v1" })).toBe(false);
  });
});
