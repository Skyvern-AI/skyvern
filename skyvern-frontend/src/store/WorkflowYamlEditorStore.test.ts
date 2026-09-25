import {
  registerEditorOwner,
  unregisterEditorOwner,
  beginSaveTransaction,
  finishSaveTransaction,
  isLockedByOther,
  disposeWorkflowAuthoringSubscriptions,
  registerWorkflowAuthoringSubscriptions,
  isWorkflowYamlDirty,
  persistYamlCommitIfCurrent,
  refuseMutationDuringYamlCommit,
  commitYamlDraft,
  subscribeToYamlDraftChanges,
  useWorkflowYamlEditorStore,
  beginYamlCommit,
  createYamlCommitOwner,
  finishYamlCommit,
  invalidateYamlCommitOwner,
  beginCopilotAcceptance,
  finishCopilotAcceptance,
  withCopilotAcceptance,
  filterWorkflowChanges,
  getWorkflowLockMessage,
  isWorkflowMutation,
} from "./WorkflowYamlEditorStore";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { toast } from "@/components/ui/use-toast";
import type { NodeChange, EdgeChange } from "@xyflow/react";
import { useWorkflowHasChangesStore } from "./WorkflowHasChangesStore";
import { useWorkflowTitleStore } from "./WorkflowTitleStore";
import { useRecordingStore } from "./useRecordingStore";
import { useRecordedBlocksStore } from "./RecordedBlocksStore";
import { useWorkflowParametersStore } from "./WorkflowParametersStore";

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

describe("Copilot turn reservations", () => {
  beforeEach(() => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    useWorkflowTitleStore.setState(useWorkflowTitleStore.getInitialState());
    useWorkflowParametersStore.setState(
      useWorkflowParametersStore.getInitialState(),
    );
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
  });

  afterEach(() => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
  });

  test("refuses ordinary editor writes while a Copilot turn is reserved", () => {
    const store = useWorkflowYamlEditorStore.getState();
    store.open("blocks: []");
    const token = beginCopilotAcceptance()!;
    const parameter = {
      key: "input",
      parameterType: "context" as const,
      sourceParameterKey: "source",
    };
    useWorkflowParametersStore.getState().setParameters([parameter]);
    useWorkflowTitleStore.getState().setTitle("User edit");
    useWorkflowTitleStore.getState().setDescriptionFromUser("User description");
    useWorkflowHasChangesStore.getState().setHasChanges(true);
    store.setDraft("blocks: []\ntitle: YAML edit");
    expect(useWorkflowParametersStore.getState().parameters).toEqual([]);
    expect(useWorkflowTitleStore.getState()).toMatchObject({
      title: "",
      description: null,
    });
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
    expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
      draft: "blocks: []",
      copilotAcceptance: token,
      commitInProgress: false,
    });
    expect(useWorkflowYamlEditorStore.getState().revision).toBe(store.revision);
    expect(refuseMutationDuringYamlCommit()).toBe(true);
    finishCopilotAcceptance(token);
  });

  test("defers YAML commits and Copilot reservations until authoring finishes", async () => {
    const commit = vi.fn().mockResolvedValue(true);
    const owner = createYamlCommitOwner("wpid_1");
    useWorkflowYamlEditorStore.setState({ authoringInProgress: true, commit });

    expect(await commitYamlDraft(true)).toBe(false);
    expect(beginYamlCommit(owner)).toBe(false);
    expect(beginCopilotAcceptance()).toBeNull();
    expect(commit).not.toHaveBeenCalled();
    useWorkflowHasChangesStore.getState().setHasChanges(true);
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);

    useWorkflowYamlEditorStore.setState({ authoringInProgress: false });
    expect(await commitYamlDraft(true)).toBe(true);
    const token = beginCopilotAcceptance();
    expect(token).not.toBeNull();
    finishCopilotAcceptance(token!);
  });

  test("silently passes passive graph events, refuses mutations, and permits owner graph writes", () => {
    const passive: Array<NodeChange | EdgeChange> = [
      { type: "select", id: "node", selected: true },
      { type: "dimensions", id: "node", dimensions: { width: 10, height: 20 } },
      {
        type: "position",
        id: "node",
        dragging: true,
        position: { x: 1, y: 2 },
      },
      {
        type: "position",
        id: "node",
        dragging: false,
        position: { x: 3, y: 4 },
      },
      { type: "position", id: "node", position: { x: 5, y: 6 } },
    ];
    expect(passive.some(isWorkflowMutation)).toBe(false);
    const mutations: Array<NodeChange | EdgeChange> = [
      { type: "add", item: { id: "new", data: {}, position: { x: 0, y: 0 } } },
      { type: "remove", id: "node" },
      {
        type: "replace",
        id: "node",
        item: {
          id: "node",
          data: { label: "edited" },
          position: { x: 0, y: 0 },
        },
      },
      { type: "add", item: { id: "edge", source: "a", target: "b" } },
    ];
    const token = beginCopilotAcceptance()!;
    vi.mocked(toast).mockClear();
    expect(filterWorkflowChanges(passive)).toEqual(passive);
    expect(toast).not.toHaveBeenCalled();
    expect(filterWorkflowChanges([...passive, ...mutations])).toEqual(passive);
    expect(toast).toHaveBeenCalledExactlyOnceWith({
      title: "Wait for the Copilot change to finish",
      variant: "destructive",
    });
    withCopilotAcceptance(token, () => {
      expect(filterWorkflowChanges(mutations)).toEqual(mutations);
      const parameters = useWorkflowParametersStore.getState();
      parameters.setParameters([
        {
          key: "owned",
          parameterType: "context",
          sourceParameterKey: "source",
        },
      ]);
      useWorkflowTitleStore
        .getState()
        .setDescriptionFromUser("Owned description");
      useWorkflowHasChangesStore.getState().setHasChanges(true);
    });
    expect(useWorkflowParametersStore.getState().parameters[0]?.key).toBe(
      "owned",
    );
    expect(useWorkflowTitleStore.getState().description).toBe(
      "Owned description",
    );
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    finishCopilotAcceptance(token);
    expect(filterWorkflowChanges(mutations)).toEqual(mutations);
  });

  test.each([
    ["yaml", "A YAML commit is in progress"],
    ["save", "A save is in progress"],
    ["copilot", "Wait for the Copilot change to finish"],
  ] as const)("reports the %s lock kind", (lockKind, message) => {
    useWorkflowYamlEditorStore.setState({
      lockKind,
      commitInProgress: lockKind !== "copilot",
      copilotAcceptance: lockKind === "copilot" ? Symbol("turn") : null,
    });
    expect(getWorkflowLockMessage()).toBe(message);
  });

  test("allows the owning apply's nested writes and refuses unrelated or stale applies", () => {
    const token = beginCopilotAcceptance()!;
    const otherWriter = vi.fn();
    expect(withCopilotAcceptance(undefined, otherWriter)).toBe(false);
    expect(withCopilotAcceptance(Symbol("stale"), otherWriter)).toBe(false);
    expect(otherWriter).not.toHaveBeenCalled();
    expect(beginCopilotAcceptance()).toBeNull();
    expect(
      withCopilotAcceptance(token, () => {
        expect(refuseMutationDuringYamlCommit()).toBe(false);
        useWorkflowTitleStore.getState().setTitle("Owned edit");
      }),
    ).toBe(true);
    expect(useWorkflowTitleStore.getState().title).toBe("Owned edit");
    finishCopilotAcceptance(Symbol("stale"));
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(token);
    finishCopilotAcceptance(token);
    expect(withCopilotAcceptance(token, otherWriter)).toBe(false);
    const nextToken = beginCopilotAcceptance()!;
    expect(withCopilotAcceptance(token, otherWriter)).toBe(false);
    expect(otherWriter).not.toHaveBeenCalled();
    finishCopilotAcceptance(nextToken);
    expect(withCopilotAcceptance(undefined, otherWriter)).toBe(true);
    expect(otherWriter).toHaveBeenCalledTimes(1);
  });

  test("an apply exception restores the guard and retains the reservation", () => {
    const token = beginCopilotAcceptance()!;
    expect(() =>
      withCopilotAcceptance(token, () => {
        throw new Error("apply failed");
      }),
    ).toThrow("apply failed");
    expect(useWorkflowYamlEditorStore.getState().copilotAcceptance).toBe(token);
    expect(beginYamlCommit(createYamlCommitOwner("wpid_1"))).toBe(false);
    const otherWriter = vi.fn();
    expect(withCopilotAcceptance(undefined, otherWriter)).toBe(false);
    expect(otherWriter).not.toHaveBeenCalled();
    useWorkflowTitleStore.getState().setTitle("Edit after apply failure");
    expect(useWorkflowTitleStore.getState().title).toBe("");
    finishCopilotAcceptance(token);
  });

  test("resetting the store removes the active Copilot write scope", () => {
    const token = beginCopilotAcceptance()!;
    withCopilotAcceptance(token, () => {
      expect(isLockedByOther()).toBe(false);
      useWorkflowYamlEditorStore.setState(
        useWorkflowYamlEditorStore.getInitialState(),
      );
      useWorkflowYamlEditorStore.setState({
        copilotAcceptance: token,
        lockKind: "copilot",
      });
      expect(isLockedByOther()).toBe(true);
      expect(refuseMutationDuringYamlCommit()).toBe(true);
    });
  });
});

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

describe("YAML commit concurrency", () => {
  beforeEach(() => {
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
  });

  test("an invalidated editor cannot apply a delayed save or release the next editor's lock", async () => {
    const owner = createYamlCommitOwner("wpid_before");
    const store = useWorkflowYamlEditorStore.getState();
    const apply = vi.fn();
    let resolveSave!: () => void;
    store.registerCommit(async () => {
      if (!beginYamlCommit(owner)) return false;
      try {
        const saved = await persistYamlCommitIfCurrent(
          store.revision,
          () =>
            new Promise<void>((resolve) => {
              resolveSave = resolve;
            }),
          owner,
        );
        if (!saved) return false;
        apply();
        return true;
      } finally {
        finishYamlCommit(owner);
      }
    });
    const committing = commitYamlDraft(true);
    invalidateYamlCommitOwner(owner);
    expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(false);
    const nextOwner = createYamlCommitOwner("wpid_after");
    expect(beginYamlCommit(nextOwner)).toBe(true);
    resolveSave();
    expect(await committing).toBe(false);
    expect(apply).not.toHaveBeenCalled();
    expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
      commitOwner: nextOwner,
      commitInProgress: true,
      error: null,
    });
    finishYamlCommit(nextOwner);
  });

  test("parameter add, edit, and delete are refused during delayed persistence", async () => {
    const parameters = useWorkflowParametersStore.getState();
    const original = {
      key: "input",
      parameterType: "context" as const,
      sourceParameterKey: "source",
    };
    parameters.setParameters([original]);
    const store = useWorkflowYamlEditorStore.getState();
    const owner = createYamlCommitOwner("wpid_parameters");
    beginYamlCommit(owner);
    let resolveSave!: () => void;
    const saving = persistYamlCommitIfCurrent(
      store.revision,
      () =>
        new Promise<void>((resolve) => {
          resolveSave = resolve;
        }),
      owner,
    );
    parameters.setParameters([original, { ...original, key: "new_input" }]);
    parameters.setParameters([{ ...original, key: "renamed_input" }]);
    parameters.setParameters([]);
    expect(useWorkflowParametersStore.getState().parameters).toEqual([
      original,
    ]);
    expect(useWorkflowYamlEditorStore.getState().revision).toBe(store.revision);
    resolveSave();
    expect(await saving).toEqual({ response: undefined });
    finishYamlCommit(owner);
    parameters.setParameters([]);
    expect(useWorkflowParametersStore.getState().parameters).toEqual([]);
  });

  test("a revision change after persistence starts retains the draft and declines apply", async () => {
    const store = useWorkflowYamlEditorStore.getState();
    store.open("blocks: []");
    store.setDraft("blocks: []\ntitle: Edited");
    const owner = createYamlCommitOwner("wpid_changed");
    beginYamlCommit(owner);
    let resolveSave!: () => void;
    const saving = persistYamlCommitIfCurrent(
      store.revision,
      () =>
        new Promise<void>((resolve) => {
          resolveSave = resolve;
        }),
      owner,
    );
    store.bumpRevision();
    resolveSave();
    expect(await saving).toBe(false);
    expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
      active: true,
      draft: "blocks: []\ntitle: Edited",
      error:
        "Saved on the server, but local edits changed during the save; reload.",
    });
    finishYamlCommit(owner);
  });

  test("live mutation entry points advance the revision even when already dirty", () => {
    const revision = () => useWorkflowYamlEditorStore.getState().revision;
    const changes = useWorkflowHasChangesStore.getState();
    changes.setHasChanges(true);
    let previous = revision();
    changes.setHasChanges(true);
    expect(revision()).toBeGreaterThan(previous);
    previous = revision();
    changes.setGetSaveData(vi.fn());
    expect(revision()).toBe(previous);
    previous = revision();
    useWorkflowTitleStore.getState().setTitle("Revised title");
    expect(revision()).toBeGreaterThan(previous);
    previous = revision();
    useWorkflowTitleStore
      .getState()
      .setDescriptionFromUser("Revised description");
    expect(revision()).toBeGreaterThan(previous);
  });

  test("metadata edits cannot overwrite an in-flight save, but its own apply can", async () => {
    const titles = useWorkflowTitleStore.getState();
    titles.setTitle("Before");
    titles.setDescriptionFromUser("Before description");
    const store = useWorkflowYamlEditorStore.getState();
    store.setCommitInProgress(true);
    await persistYamlCommitIfCurrent(store.revision, async () => {
      titles.setTitle("Competing rename");
      titles.setDescriptionFromUser("Competing description");
      titles.setTitleFromGeneration("Late generated title");
      expect(useWorkflowTitleStore.getState()).toMatchObject({
        title: "Before",
        description: "Before description",
      });
    });
    titles.setTitle("New Workflow", { fromYamlCommit: true });
    titles.setDescriptionFromUser("", { fromYamlCommit: true });
    expect(useWorkflowTitleStore.getState()).toMatchObject({
      title: "New Workflow",
      description: null,
      titleHasBeenGenerated: false,
    });
    store.setCommitInProgress(false);
  });

  test("a revision bump during conversion prevents the save mutation", async () => {
    const store = useWorkflowYamlEditorStore.getState();
    const revisionAtEntry = store.revision;
    store.setCommitInProgress(true);
    await Promise.resolve().then(() => store.bumpRevision());
    const save = vi.fn().mockResolvedValue(undefined);
    expect(await persistYamlCommitIfCurrent(revisionAtEntry, save)).toBe(false);
    expect(save).not.toHaveBeenCalled();
    store.setCommitInProgress(false);
  });

  test("refuses competing apply and rejection restore throughout persistence", async () => {
    let resolveSave!: () => void;
    const save = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveSave = resolve;
        }),
    );
    const store = useWorkflowYamlEditorStore.getState();
    store.setCommitInProgress(true);
    const saving = persistYamlCommitIfCurrent(store.revision, save);
    const apply = vi.fn();
    const restore = vi.fn();
    if (!refuseMutationDuringYamlCommit()) apply();
    if (!refuseMutationDuringYamlCommit()) restore();
    expect(apply).not.toHaveBeenCalled();
    expect(restore).not.toHaveBeenCalled();
    expect(useWorkflowYamlEditorStore.getState().committing).toBe(true);
    resolveSave();
    expect(await saving).toEqual({ response: undefined });
    store.setCommitInProgress(false);
    expect(refuseMutationDuringYamlCommit()).toBe(false);
    expect(useWorkflowYamlEditorStore.getState().committing).toBe(false);
  });

  test("the commit entry refuses reentrancy and clears its UI flag after failure", async () => {
    const store = useWorkflowYamlEditorStore.getState();
    const commit = vi.fn(async () => {
      store.setCommitInProgress(true);
      throw new Error("failure");
    });
    store.registerCommit(commit);
    store.setCommitInProgress(true);
    expect(await commitYamlDraft(true)).toBe(false);
    expect(commit).not.toHaveBeenCalled();
    store.setCommitInProgress(false);
    await expect(commitYamlDraft(true)).rejects.toThrow("failure");
    expect(useWorkflowYamlEditorStore.getState().committing).toBe(false);
    expect(useWorkflowYamlEditorStore.getState().commitInProgress).toBe(false);
  });
});

describe("authoring subscription lifecycle", () => {
  test("disposes both subscriptions and re-registers them only once", () => {
    disposeWorkflowAuthoringSubscriptions();
    const recordingSubscribe = vi.spyOn(useRecordingStore, "subscribe");
    const blocksSubscribe = vi.spyOn(useRecordedBlocksStore, "subscribe");
    try {
      registerWorkflowAuthoringSubscriptions();
      registerWorkflowAuthoringSubscriptions();
      expect(recordingSubscribe).toHaveBeenCalledOnce();
      expect(blocksSubscribe).toHaveBeenCalledOnce();
      useRecordingStore.setState({ isRecording: true });
      expect(useWorkflowYamlEditorStore.getState().authoringInProgress).toBe(
        true,
      );
      disposeWorkflowAuthoringSubscriptions();
      useRecordingStore.setState({ isRecording: false });
      expect(useWorkflowYamlEditorStore.getState().authoringInProgress).toBe(
        true,
      );
      registerWorkflowAuthoringSubscriptions();
      expect(useWorkflowYamlEditorStore.getState().authoringInProgress).toBe(
        false,
      );
      expect(recordingSubscribe).toHaveBeenCalledTimes(2);
      expect(blocksSubscribe).toHaveBeenCalledTimes(2);
    } finally {
      recordingSubscribe.mockRestore();
      blocksSubscribe.mockRestore();
      useRecordingStore.getState().reset();
      useWorkflowYamlEditorStore.setState(
        useWorkflowYamlEditorStore.getInitialState(),
      );
    }
  });
});

describe("save ownership", () => {
  beforeEach(() =>
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    ),
  );
  afterEach(() =>
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    ),
  );
  test.each([
    ["save", beginSaveTransaction, "A save is in progress"],
    ["yaml", beginYamlCommit, "A YAML commit is in progress"],
  ] as const)(
    "names the %s lock holder until release",
    (kind, begin, message) => {
      const owner = createYamlCommitOwner("wpid_1");
      registerEditorOwner(owner);
      expect(begin(owner)).toBe(true);
      expect(beginSaveTransaction(owner)).toBe(false);
      expect(beginYamlCommit(owner)).toBe(false);
      expect(useWorkflowYamlEditorStore.getState().lockKind).toBe(kind);
      expect(refuseMutationDuringYamlCommit()).toBe(true);
      expect(toast).toHaveBeenLastCalledWith({
        title: message,
        variant: "destructive",
      });
      finishSaveTransaction(owner);
      expect(useWorkflowYamlEditorStore.getState().lockKind).toBeNull();
      expect(refuseMutationDuringYamlCommit()).toBe(false);
      expect(beginYamlCommit(owner)).toBe(true);
      expect(refuseMutationDuringYamlCommit()).toBe(true);
      expect(toast).toHaveBeenLastCalledWith({
        title: "A YAML commit is in progress",
        variant: "destructive",
      });
      unregisterEditorOwner(owner);
      expect(useWorkflowYamlEditorStore.getState().lockKind).toBeNull();
    },
  );
  test.each([
    ["save", beginSaveTransaction],
    ["yaml", beginYamlCommit],
  ] as const)(
    "excludes %s transactions and Copilot reservations, while allowing the owning restore",
    async (_kind, begin) => {
      const owner = createYamlCommitOwner("wpid_1");
      registerEditorOwner(owner);
      const token = beginCopilotAcceptance()!;
      expect(begin(owner)).toBe(false);
      const commit = vi.fn(async () => true);
      useWorkflowYamlEditorStore.getState().registerCommit(commit);
      expect(await commitYamlDraft(true)).toBe(false);
      expect(commit).not.toHaveBeenCalled();
      expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
        copilotAcceptance: token,
        commitInProgress: false,
      });
      expect(isLockedByOther(token)).toBe(false);
      expect(isLockedByOther()).toBe(true);
      finishCopilotAcceptance(token);
      expect(begin(owner)).toBe(true);
      expect(beginCopilotAcceptance()).toBeNull();
      expect(begin(owner)).toBe(false);
      expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
        copilotAcceptance: null,
        commitInProgress: true,
      });
      expect(isLockedByOther(token)).toBe(true);
      finishSaveTransaction(owner);
      expect(isLockedByOther()).toBe(false);
      unregisterEditorOwner(owner);
      expect(beginSaveTransaction(owner)).toBe(false);
    },
  );
  test("unregistering an older Workspace cannot release the current owner's lock", () => {
    const old = createYamlCommitOwner("wpid_old");
    const current = createYamlCommitOwner("wpid_current");
    registerEditorOwner(old);
    registerEditorOwner(current);
    expect(beginSaveTransaction(current)).toBe(true);
    unregisterEditorOwner(old);
    expect(useWorkflowYamlEditorStore.getState()).toMatchObject({
      editorOwner: current,
      commitOwner: current,
      commitInProgress: true,
    });
    unregisterEditorOwner(current);
  });
});
