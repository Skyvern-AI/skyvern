import { create } from "zustand";

const STORAGE_KEY = "skyvern.workflowHeader.collapsed";

const readInitial = (): boolean => {
  if (typeof window === "undefined") {
    return false;
  }
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "true";
  } catch {
    return false;
  }
};

const persist = (collapsed: boolean): void => {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(STORAGE_KEY, String(collapsed));
  } catch {
    // localStorage may be unavailable (private mode, quota); ignore.
  }
};

type WorkflowHeaderCollapseState = {
  collapsed: boolean;
  toggle: () => void;
  setCollapsed: (collapsed: boolean) => void;
};

export const useWorkflowHeaderCollapseStore =
  create<WorkflowHeaderCollapseState>((set, get) => ({
    collapsed: readInitial(),
    toggle: () => {
      const next = !get().collapsed;
      persist(next);
      set({ collapsed: next });
    },
    setCollapsed: (collapsed) => {
      persist(collapsed);
      set({ collapsed });
    },
  }));
