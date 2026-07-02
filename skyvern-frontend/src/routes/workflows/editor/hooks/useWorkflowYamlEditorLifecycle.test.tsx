import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";

import { useWorkflowYamlEditorLifecycle } from "./useWorkflowYamlEditorLifecycle";

describe("useWorkflowYamlEditorLifecycle", () => {
  beforeEach(() => {
    useWorkflowYamlEditorStore.getState().close();
    useWorkflowYamlEditorStore.getState().registerCommit(null);
  });

  test("registers a wrapper that always calls the latest commit closure", async () => {
    const first = vi.fn().mockResolvedValue(true);
    const second = vi.fn().mockResolvedValue(false);
    const { rerender } = renderHook(
      ({ commit }: { commit: (persist?: boolean) => Promise<boolean> }) =>
        useWorkflowYamlEditorLifecycle(commit),
      { initialProps: { commit: first } },
    );

    await useWorkflowYamlEditorStore.getState().commit?.(true);
    expect(first).toHaveBeenCalledWith(true);

    rerender({ commit: second });
    await useWorkflowYamlEditorStore.getState().commit?.(false);
    expect(second).toHaveBeenCalledWith(false);
    expect(first).toHaveBeenCalledTimes(1);
  });

  // Pins the unmount cleanup: the store is global, so a dirty draft left
  // active after unmount would leak into the next workflow's editor and could
  // be committed there.
  test("unmount closes the editor so a dirty draft cannot leak across workflows", () => {
    const { unmount } = renderHook(() =>
      useWorkflowYamlEditorLifecycle(async () => true),
    );

    useWorkflowYamlEditorStore.getState().open("blocks: []");
    useWorkflowYamlEditorStore.getState().setDraft("blocks: [changed]");
    expect(useWorkflowYamlEditorStore.getState().active).toBe(true);

    unmount();

    const state = useWorkflowYamlEditorStore.getState();
    expect(state.active).toBe(false);
    expect(state.draft).toBe("");
    expect(state.entrySnapshot).toBe("");
    expect(state.commit).toBeNull();
  });
});
