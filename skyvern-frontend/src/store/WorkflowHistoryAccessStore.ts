import { create } from "zustand";

type HistoryAccess = {
  canUndo: boolean;
  canRedo: boolean;
  undo: () => void;
  redo: () => void;
  captureImmediately: () => void;
};

type WorkflowHistoryAccessStore = HistoryAccess & {
  setHistoryAccess: (access: HistoryAccess) => void;
  reset: () => void;
};

const noop = () => {};

const initial: HistoryAccess = {
  canUndo: false,
  canRedo: false,
  undo: noop,
  redo: noop,
  captureImmediately: noop,
};

const useWorkflowHistoryAccessStore = create<WorkflowHistoryAccessStore>(
  (set) => {
    return {
      ...initial,
      setHistoryAccess: (access: HistoryAccess) => {
        set(access);
      },
      reset: () => {
        set(initial);
      },
    };
  },
);

export { useWorkflowHistoryAccessStore };
