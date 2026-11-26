import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useEmberActionStore } from "@/store/useEmberActionStore";
import {
  PENDING_GOAL_SAVE_MESSAGE,
  useGuardedWorkflowSave,
} from "./useGuardedWorkflowSave";

const save = vi.hoisted(() => vi.fn(async () => {}));
vi.mock("./useSaveWorkflow", () => ({ useSaveWorkflow: () => save }));
vi.mock("@/routes/workflows/hooks/useIsGlobalWorkflow", () => ({
  useIsGlobalWorkflow: () => false,
}));

afterEach(() => {
  cleanup();
  useEmberActionStore.getState().setPendingGoalChanges([]);
  save.mockClear();
});

it("guards trigger-dialog saves using the current Ember action store", async () => {
  useEmberActionStore
    .getState()
    .setPendingGoalChanges([
      { label: "invoice", goal: "Read the invoice", previousGoal: null },
    ]);
  const { result } = renderHook(() => useGuardedWorkflowSave());
  expect(result.current.pendingGoalChanges).toBe(true);
  await expect(result.current.save()).rejects.toThrow(
    PENDING_GOAL_SAVE_MESSAGE,
  );
  expect(save).not.toHaveBeenCalled();
  act(() => useEmberActionStore.getState().setPendingGoalChanges([]));
  await result.current.save();
  expect(save).toHaveBeenCalledOnce();
});
