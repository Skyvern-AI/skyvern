import { beforeEach, describe, expect, test, vi } from "vitest";

import {
  isWorkflowYamlDirty,
  subscribeToYamlDraftChanges,
  useWorkflowYamlEditorStore,
} from "./WorkflowYamlEditorStore";

describe("WorkflowYamlEditorStore", () => {
  beforeEach(() => {
    // close() intentionally preserves the registered commit, so reset it
    // separately to keep tests isolated.
    useWorkflowYamlEditorStore.getState().close();
    useWorkflowYamlEditorStore.getState().registerCommit(null);
  });

  test("open activates the editor and snapshots the draft", () => {
    useWorkflowYamlEditorStore.getState().open("title: hello");
    const state = useWorkflowYamlEditorStore.getState();
    expect(state.active).toBe(true);
    expect(state.draft).toBe("title: hello");
    expect(state.entrySnapshot).toBe("title: hello");
    expect(state.error).toBeNull();
    expect(isWorkflowYamlDirty(state)).toBe(false);
  });

  test("editing the draft after open marks it dirty", () => {
    useWorkflowYamlEditorStore.getState().open("blocks: []");
    useWorkflowYamlEditorStore.getState().setDraft("blocks: [a]");
    expect(isWorkflowYamlDirty(useWorkflowYamlEditorStore.getState())).toBe(
      true,
    );
  });

  test("setDraft clears a previously surfaced error", () => {
    useWorkflowYamlEditorStore.getState().open("a: 1");
    useWorkflowYamlEditorStore.getState().setError("could not parse");
    expect(useWorkflowYamlEditorStore.getState().error).toBe("could not parse");
    useWorkflowYamlEditorStore.getState().setDraft("a: 2");
    expect(useWorkflowYamlEditorStore.getState().error).toBeNull();
  });

  test("close resets session state but preserves the registered commit", () => {
    const commit = async () => true;
    useWorkflowYamlEditorStore.getState().registerCommit(commit);
    useWorkflowYamlEditorStore.getState().open("a: 1");
    useWorkflowYamlEditorStore.getState().setDraft("a: 2");
    useWorkflowYamlEditorStore.getState().close();
    const state = useWorkflowYamlEditorStore.getState();
    expect(state.active).toBe(false);
    expect(state.draft).toBe("");
    expect(state.entrySnapshot).toBe("");
    expect(state.committing).toBe(false);
    // Workspace registers commit once on mount; close must not drop it or a
    // second open would have no way to reparse.
    expect(state.commit).toBe(commit);
  });
});

describe("subscribeToYamlDraftChanges", () => {
  beforeEach(() => {
    useWorkflowYamlEditorStore.getState().close();
  });

  test("fires on a draft edit while the editor is active", () => {
    useWorkflowYamlEditorStore.getState().open("blocks: []");
    const onChange = vi.fn();
    const unsub = subscribeToYamlDraftChanges(onChange);
    useWorkflowYamlEditorStore.getState().setDraft("blocks: [a]");
    unsub();
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  test("does not fire while the editor is inactive", () => {
    const onChange = vi.fn();
    const unsub = subscribeToYamlDraftChanges(onChange);
    // active=false; setDraft still mutates the field, but the guard skips it.
    useWorkflowYamlEditorStore.getState().setDraft("blocks: [a]");
    unsub();
    expect(onChange).not.toHaveBeenCalled();
  });

  test("does not fire when the draft is unchanged (error-only update)", () => {
    useWorkflowYamlEditorStore.getState().open("a: 1");
    const onChange = vi.fn();
    const unsub = subscribeToYamlDraftChanges(onChange);
    useWorkflowYamlEditorStore.getState().setError("could not parse");
    unsub();
    expect(onChange).not.toHaveBeenCalled();
  });

  test("stops firing after unsubscribe", () => {
    useWorkflowYamlEditorStore.getState().open("blocks: []");
    const onChange = vi.fn();
    subscribeToYamlDraftChanges(onChange)();
    useWorkflowYamlEditorStore.getState().setDraft("blocks: [a]");
    expect(onChange).not.toHaveBeenCalled();
  });
});
