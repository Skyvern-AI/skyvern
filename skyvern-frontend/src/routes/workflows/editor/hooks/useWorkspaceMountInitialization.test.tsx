import { QueryClient } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { useSidebarStore } from "@/store/SidebarStore";

import { useWorkspaceMountInitialization } from "./useWorkspaceMountInitialization";

afterEach(() => {
  useSidebarStore.setState({ collapsed: false });
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe("useWorkspaceMountInitialization", () => {
  test("keeps the global sidebar expanded when the workflow builder opens", () => {
    const queryClient = new QueryClient();
    const invalidateQueries = vi.spyOn(queryClient, "invalidateQueries");
    const workflowChangesStore = { setHasChanges: vi.fn() };
    const closeWorkflowPanel = vi.fn();
    useSidebarStore.setState({ collapsed: false });

    renderHook(() =>
      useWorkspaceMountInitialization({
        cacheKey: "default",
        closeWorkflowPanel,
        queryClient,
        workflowChangesStore,
        workflowPermanentId: "wpid_abc",
      }),
    );

    expect(useSidebarStore.getState().collapsed).toBe(false);
    expect(workflowChangesStore.setHasChanges).toHaveBeenCalledWith(false);
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["cache-key-values", "wpid_abc", "default"],
    });
    expect(closeWorkflowPanel).toHaveBeenCalledOnce();
  });
});
