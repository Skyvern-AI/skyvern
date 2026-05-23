import { beforeEach, describe, expect, test } from "vitest";

import { useWorkflowHeaderCollapseStore } from "./useWorkflowHeaderCollapseStore";

const STORAGE_KEY = "skyvern.workflowHeader.collapsed";

beforeEach(() => {
  window.localStorage.clear();
  useWorkflowHeaderCollapseStore.setState({ collapsed: false });
});

describe("useWorkflowHeaderCollapseStore", () => {
  test("starts expanded by default", () => {
    expect(useWorkflowHeaderCollapseStore.getState().collapsed).toBe(false);
  });

  test("toggle flips collapsed state", () => {
    const { toggle } = useWorkflowHeaderCollapseStore.getState();
    toggle();
    expect(useWorkflowHeaderCollapseStore.getState().collapsed).toBe(true);
    toggle();
    expect(useWorkflowHeaderCollapseStore.getState().collapsed).toBe(false);
  });

  test("toggle persists collapsed state to localStorage", () => {
    useWorkflowHeaderCollapseStore.getState().toggle();
    expect(localStorage.getItem(STORAGE_KEY)).toBe("true");
    useWorkflowHeaderCollapseStore.getState().toggle();
    expect(localStorage.getItem(STORAGE_KEY)).toBe("false");
  });

  test("setCollapsed writes the value to localStorage", () => {
    useWorkflowHeaderCollapseStore.getState().setCollapsed(true);
    expect(useWorkflowHeaderCollapseStore.getState().collapsed).toBe(true);
    expect(localStorage.getItem(STORAGE_KEY)).toBe("true");
  });
});
