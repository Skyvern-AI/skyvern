// @vitest-environment jsdom

import { act, renderHook } from "@testing-library/react";
import { type ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, test } from "vitest";

import { useStudioShellStore } from "@/store/StudioShellStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import type { WorkflowVersion } from "@/routes/workflows/hooks/useWorkflowVersionsQuery";

import { useSettingsSidebarLayout } from "./useSettingsSidebarLayout";

function wrapper({ children }: { children: ReactNode }) {
  return (
    <MemoryRouter initialEntries={["/workflows/wpid_abc/edit"]}>
      {children}
    </MemoryRouter>
  );
}

beforeEach(() => {
  useWorkflowPanelStore.getState().setSelectedBlockId(null);
  useWorkflowPanelStore.getState().setWorkflowPanelState({
    active: false,
    content: "parameters",
  });
  useStudioShellStore.getState().reset();
});

describe("useSettingsSidebarLayout", () => {
  test("closed when nothing is selected and no library is open", () => {
    const { result } = renderHook(() => useSettingsSidebarLayout(), {
      wrapper,
    });
    expect(result.current).toEqual({
      open: false,
      isLibrary: false,
      nodeSelected: false,
      collapsed: false,
    });
  });

  test("a selected block opens the column collapsed (studio starts collapsed)", () => {
    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("block-a");
    });
    const { result } = renderHook(() => useSettingsSidebarLayout(), {
      wrapper,
    });
    expect(result.current.open).toBe(true);
    expect(result.current.nodeSelected).toBe(true);
    expect(result.current.isLibrary).toBe(false);
    expect(result.current.collapsed).toBe(true);
  });

  test("expanding the studio settings clears collapsed", () => {
    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("block-a");
      useStudioShellStore.getState().setSettingsCollapsed(false);
    });
    const { result } = renderHook(() => useSettingsSidebarLayout(), {
      wrapper,
    });
    expect(result.current.open).toBe(true);
    expect(result.current.collapsed).toBe(false);
  });

  test("the comparison view keeps the column closed even with a block selected", () => {
    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("block-a");
      useWorkflowPanelStore.getState().setWorkflowPanelState({
        active: true,
        content: "parameters",
        data: {
          showComparison: true,
          version1: {} as WorkflowVersion,
          version2: {} as WorkflowVersion,
        },
      });
    });
    const { result } = renderHook(() => useSettingsSidebarLayout(), {
      wrapper,
    });
    expect(result.current.open).toBe(false);
    expect(result.current.nodeSelected).toBe(false);
    expect(result.current.collapsed).toBe(false);
  });

  test("the node library opens the column but is never collapsible", () => {
    act(() => {
      useWorkflowPanelStore.getState().setWorkflowPanelState({
        active: true,
        content: "nodeLibrary",
      });
      // ...even while the studio is in its collapsed state.
      useStudioShellStore.getState().setSettingsCollapsed(true);
    });
    const { result } = renderHook(() => useSettingsSidebarLayout(), {
      wrapper,
    });
    expect(result.current.open).toBe(true);
    expect(result.current.isLibrary).toBe(true);
    expect(result.current.nodeSelected).toBe(false);
    expect(result.current.collapsed).toBe(false);
  });
});
