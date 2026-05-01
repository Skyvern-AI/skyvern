import { create } from "zustand";

interface DebuggerLastRunValuesStore {
  valuesByWorkflowId: Record<string, Record<string, unknown>>;
  getLastRunValues: (
    workflowPermanentId: string,
  ) => Record<string, unknown> | null;
  setLastRunValues: (
    workflowPermanentId: string,
    values: Record<string, unknown>,
  ) => void;
}

export const useDebuggerLastRunValuesStore = create<DebuggerLastRunValuesStore>(
  (set, get) => ({
    valuesByWorkflowId: {},
    getLastRunValues: (workflowPermanentId) =>
      get().valuesByWorkflowId[workflowPermanentId] ?? null,
    setLastRunValues: (workflowPermanentId, values) =>
      set((state) => ({
        valuesByWorkflowId: {
          ...state.valuesByWorkflowId,
          [workflowPermanentId]: values,
        },
      })),
  }),
);
