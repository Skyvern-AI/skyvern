import { act, renderHook } from "@testing-library/react";
import { beforeEach, afterEach, expect, test, vi } from "vitest";
import { WorkflowParameterValueType } from "../types/workflowTypes";
import type { AppNode } from "./nodes";
import { captureEditorState, restoreEditorState } from "./editorStateSnapshot";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import {
  beginCopilotAcceptance,
  beginSaveTransaction,
  finishCopilotAcceptance,
  finishSaveTransaction,
  isLockedByOther,
  registerEditorOwner,
  useWorkflowYamlEditorStore,
  withCopilotAcceptance,
} from "@/store/WorkflowYamlEditorStore";
import { useWorkflowSnapshotStore } from "@/store/WorkflowSnapshotStore";
import { getWorkflowSettings } from "./workflowEditorUtils";
import { useNodeCollapseStore } from "./collapse/useNodeCollapseStore";
import { useAutoGenerateWorkflowTitle } from "../hooks/useAutoGenerateWorkflowTitle";

const { post } = vi.hoisted(() => ({ post: vi.fn() }));
vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn().mockResolvedValue({ post }),
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));

function fixture() {
  const node = (
    id: string,
    type: string,
    data: Record<string, unknown>,
    parentId?: string,
  ) =>
    ({
      id,
      type,
      data: { label: id, ...data },
      position: { x: 1, y: 2 },
      parentId,
      hidden: true,
    }) as AppNode;
  const nodes = [
    node("start", "start", {
      retryPolicy: { maxRetries: 4 },
      extraHttpHeaders: '{"X-Token":"raw"}',
    }),
    node("loop", "loop", {
      loopKind: "for_each",
      loopValue: "edited_items",
      loopVariableReference: "{{ item }}",
      dataSchema: "null",
    }),
    node(
      "task",
      "task",
      {
        parameterKeys: ["input"],
        url: "https://example.test",
        navigationGoal: "Read page",
      },
      "loop",
    ),
    node(
      "inner",
      "loop",
      {
        loopKind: "for_each",
        loopValue: "nested",
        loopVariableReference: "{{ nested }}",
      },
      "loop",
    ),
    node(
      "email",
      "sendEmail",
      {
        smtpHostSecretParameterKey: "host",
        smtpPortSecretParameterKey: "port",
        smtpUsernameSecretParameterKey: "user",
        smtpPasswordSecretParameterKey: "password",
      },
      "inner",
    ),
    node("conditional", "conditional", { activeBranchId: "selected" }, "loop"),
    node(
      "chosen",
      "codeBlock",
      { conditionalBranchId: "selected" },
      "conditional",
    ),
    node("other", "codeBlock", { conditionalBranchId: "other" }, "conditional"),
  ];
  const parameters = [
    {
      parameterType: "context" as const,
      key: "context",
      sourceParameterKey: "edited_source",
    },
    {
      parameterType: "workflow" as const,
      key: "flag",
      dataType: WorkflowParameterValueType.Boolean,
      defaultValue: false,
    },
    {
      parameterType: "workflow" as const,
      key: "count",
      dataType: WorkflowParameterValueType.Integer,
      defaultValue: 42,
    },
  ];
  return {
    workflowPermanentId: "wpid_1",
    nodes,
    edges: [{ id: "edge", source: "start", target: "loop" }],
    parameters,
    title: "Edited title",
    titleHasBeenGenerated: true,
    description: "Edited description",
    hasChanges: true,
    saveGeneration: 0,
  };
}
function deps(id = "wpid_1") {
  return {
    workflowPermanentId: id,
    setNodes: vi.fn(),
    setEdges: vi.fn(),
    parametersStore: useWorkflowParametersStore.getState(),
    titleStore: useWorkflowTitleStore.getState(),
    changesStore: useWorkflowHasChangesStore.getState(),
    collapseStore: useNodeCollapseStore.getState(),
    restoreOwnership: vi.fn((workflowPermanentId: string) => {
      useWorkflowParametersStore.setState({
        parametersWorkflowPermanentId: workflowPermanentId,
      });
      useWorkflowTitleStore.setState({
        titleWorkflowPermanentId: workflowPermanentId,
        descriptionWorkflowPermanentId: workflowPermanentId,
      });
    }),
    scheduleLayout: vi.fn(),
    isLockedByOther,
  };
}
beforeEach(() => {
  useWorkflowSnapshotStore.getState().clearSnapshot();
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
  useWorkflowHasChangesStore.setState(
    useWorkflowHasChangesStore.getInitialState(),
  );
  useWorkflowTitleStore.setState(useWorkflowTitleStore.getInitialState());
  useWorkflowParametersStore.setState(
    useWorkflowParametersStore.getInitialState(),
  );
  useNodeCollapseStore.setState({ collapsed: {} });
  post.mockReset();
});
afterEach(() => {
  vi.useRealTimers();
});

test("preserves the unsaved loop binding across rejection restore", () => {
  const source = fixture();
  useNodeCollapseStore.getState().collapseAll("wpid_1", ["loop"]);
  const snapshot = captureEditorState(source);
  useNodeCollapseStore.getState().expandBlock("wpid_1", "loop");
  Object.assign(source.nodes[1]!.data, { loopValue: "later edit" });
  source.parameters[0]!.key = "later key";
  useNodeCollapseStore.getState().collapseAll("wpid_1", ["inner"]);
  const target = deps();
  expect(restoreEditorState(snapshot, target)).toBe("restored");
  const restored = target.setNodes.mock.calls[0]![0] as AppNode[];
  expect(restored.find((node) => node.id === "loop")?.data).toMatchObject({
    loopValue: "edited_items",
    loopVariableReference: "{{ item }}",
  });
  expect(restored.find((node) => node.id === "task")?.data).toMatchObject({
    parameterKeys: ["input"],
  });
  expect(restored.find((node) => node.id === "email")?.data).toMatchObject({
    smtpHostSecretParameterKey: "host",
    smtpPortSecretParameterKey: "port",
    smtpUsernameSecretParameterKey: "user",
    smtpPasswordSecretParameterKey: "password",
  });
  expect(restored[0]?.data).toEqual(snapshot.nodes[0]?.data);
  expect(target.setEdges).toHaveBeenCalledWith(snapshot.edges);
  expect(useWorkflowParametersStore.getState().parameters).toEqual(
    snapshot.parameters,
  );
  expect(useWorkflowTitleStore.getState()).toMatchObject({
    title: "Edited title",
    titleHasBeenGenerated: true,
    description: "Edited description",
  });
  expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
  expect(restored.filter((node) => node.hidden).map((node) => node.id)).toEqual(
    ["email", "other"],
  );
  expect(target.scheduleLayout).toHaveBeenCalledOnce();
  Object.assign(restored[1]!.data, { loopValue: "mutated restored data" });
  expect(snapshot.nodes[1]!.data).toMatchObject({ loopValue: "edited_items" });
});

test("restores parameter ownership after the departing editor resets its session", () => {
  const snapshot = captureEditorState(fixture());
  useWorkflowParametersStore.getState().setParameters(snapshot.parameters, {
    workflowPermanentId: snapshot.workflowPermanentId,
  });
  useWorkflowParametersStore
    .getState()
    .resetParametersSession(snapshot.workflowPermanentId);
  expect(
    useWorkflowParametersStore.getState().parametersWorkflowPermanentId,
  ).toBeNull();

  expect(restoreEditorState(snapshot, deps())).toBe("restored");

  expect(
    useWorkflowParametersStore.getState().parametersWorkflowPermanentId,
  ).toBe(snapshot.workflowPermanentId);
});

test("restores metadata ownership after the departing editor resets its session", () => {
  const snapshot = captureEditorState(fixture());
  const titles = useWorkflowTitleStore.getState();
  titles.initializeTitle("Saved title", snapshot.workflowPermanentId);
  titles.initializeDescription(
    snapshot.workflowPermanentId,
    "Saved description",
  );
  titles.resetTitleSession(snapshot.workflowPermanentId);
  titles.resetDescriptionSession(snapshot.workflowPermanentId);
  expect(useWorkflowTitleStore.getState()).toMatchObject({
    titleWorkflowPermanentId: null,
    descriptionWorkflowPermanentId: null,
  });

  expect(restoreEditorState(snapshot, deps())).toBe("restored");

  expect(useWorkflowTitleStore.getState()).toMatchObject({
    title: snapshot.title,
    description: snapshot.description,
    titleWorkflowPermanentId: snapshot.workflowPermanentId,
    descriptionWorkflowPermanentId: snapshot.workflowPermanentId,
  });
});

test("restores ownership through the injected dependency without writing singleton stores", () => {
  const snapshot = captureEditorState(fixture());
  const target = {
    ...deps(),
    parametersStore: {
      ...useWorkflowParametersStore.getState(),
      setParameters: vi.fn(() => true),
    },
    titleStore: {
      ...useWorkflowTitleStore.getState(),
      restoreTitle: vi.fn(),
      setDescriptionFromWorkflow: vi.fn(),
    },
    restoreOwnership: vi.fn(),
  };
  const parameters = useWorkflowParametersStore.getState();
  const titles = useWorkflowTitleStore.getState();

  expect(restoreEditorState(snapshot, target)).toBe("restored");

  expect(target.restoreOwnership).toHaveBeenCalledExactlyOnceWith(
    snapshot.workflowPermanentId,
  );
  expect(useWorkflowParametersStore.getState()).toBe(parameters);
  expect(useWorkflowTitleStore.getState()).toBe(titles);
});

test.each([false, true])(
  "restores captured clean state only if persistence generation is unchanged (%s)",
  (persisted) => {
    const snapshot = captureEditorState({
      ...fixture(),
      hasChanges: false,
      title: "New Agent",
      titleHasBeenGenerated: false,
    });
    useWorkflowTitleStore.getState().setTitle("Generated later");
    if (persisted)
      useWorkflowHasChangesStore.getState().recordPersistedSave("wpid_1");
    expect(restoreEditorState(snapshot, deps())).toBe("restored");
    expect(useWorkflowTitleStore.getState()).toMatchObject({
      title: "New Agent",
      titleHasBeenGenerated: false,
    });
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(persisted);
  },
);

test("restores a clean snapshot after only another workflow was saved", () => {
  useWorkflowHasChangesStore.getState().recordPersistedSave("wpid_1");
  useWorkflowHasChangesStore.getState().recordPersistedSave("wpid_other");
  const snapshot = captureEditorState({
    ...fixture(),
    hasChanges: false,
    saveGeneration: useWorkflowHasChangesStore.getState().saveGeneration,
  });

  useWorkflowHasChangesStore.getState().recordPersistedSave("wpid_other");

  expect(restoreEditorState(snapshot, deps())).toBe("restored");
  expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
});

test("refuses stale workflows and other owners without partial writes", () => {
  const snapshot = captureEditorState(fixture());
  const stale = deps("wpid_other");
  expect(restoreEditorState(snapshot, stale)).toBe("refused-stale-workflow");
  expect(stale.setNodes).not.toHaveBeenCalled();
  expect(stale.restoreOwnership).not.toHaveBeenCalled();
  const token = beginCopilotAcceptance()!;
  const target = deps();
  expect(restoreEditorState(snapshot, target)).toBe("refused-locked");
  expect(target.setNodes).not.toHaveBeenCalled();
  expect(target.restoreOwnership).not.toHaveBeenCalled();
  expect(
    withCopilotAcceptance(token, () =>
      expect(restoreEditorState(snapshot, target)).toBe("restored"),
    ),
  ).toBe(true);
  finishCopilotAcceptance(token);
});

test("a title generation response cannot overwrite a restored placeholder", async () => {
  vi.useFakeTimers();
  let resolve!: (response: unknown) => void;
  post.mockImplementation(
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  const snapshot = captureEditorState({
    ...fixture(),
    title: "New Agent",
    titleHasBeenGenerated: false,
  });
  useWorkflowTitleStore.getState().restoreTitle("New Agent", false);
  const task = { ...snapshot.nodes[2]!, parentId: undefined };
  const view = renderHook(() => useAutoGenerateWorkflowTitle([task], []));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(4000);
  });
  expect(post).toHaveBeenCalledOnce();
  restoreEditorState(snapshot, deps());
  await act(async () => {
    resolve({ data: { title: "Late generated title" } });
  });
  expect(useWorkflowTitleStore.getState().title).toBe("New Agent");
  view.unmount();
});

test("applies an unlocked generated title and preserves unsaved changes", async () => {
  vi.useFakeTimers();
  post.mockResolvedValue({ data: { title: "Read page title" } });
  useWorkflowTitleStore.getState().setTitle("New Agent");
  useWorkflowHasChangesStore.getState().setHasChanges(true);
  const task = { ...fixture().nodes[2]!, parentId: undefined };
  const view = renderHook(() => useAutoGenerateWorkflowTitle([task], []));

  await act(async () => {
    await vi.advanceTimersByTimeAsync(4000);
  });

  expect(post).toHaveBeenCalledOnce();
  expect(useWorkflowTitleStore.getState().title).toBe("Read page title");
  expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
  view.unmount();
});

test.each(["save", "copilot"] as const)(
  "defers a generated title until the %s lock releases without another request",
  async (lock) => {
    vi.useFakeTimers();
    let resolve!: (response: unknown) => void;
    post.mockImplementation(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    );
    useWorkflowTitleStore.getState().setTitle("New Agent");
    useWorkflowHasChangesStore.getState().setHasChanges(true);
    const task = { ...fixture().nodes[2]!, parentId: undefined };
    const view = renderHook(() => useAutoGenerateWorkflowTitle([task], []));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3999);
    });
    expect(post).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(post).toHaveBeenCalledOnce();

    let release!: () => void;
    act(() => {
      if (lock === "save") {
        const owner = { workflowPermanentId: "wpid_1", active: true };
        registerEditorOwner(owner);
        expect(beginSaveTransaction(owner)).toBe(true);
        release = () => finishSaveTransaction(owner);
      } else {
        const token = beginCopilotAcceptance()!;
        expect(token).not.toBeNull();
        release = () => finishCopilotAcceptance(token);
      }
    });
    await act(async () => {
      resolve({ data: { title: "Read page title" } });
    });
    expect(useWorkflowTitleStore.getState().title).toBe("New Agent");
    if (lock === "save") {
      act(() => {
        useWorkflowHasChangesStore.getState().recordPersistedSave("wpid_1");
        useWorkflowHasChangesStore
          .getState()
          .setHasChanges(false, { fromYamlCommit: true });
      });
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
    }
    act(() => release());
    expect(useWorkflowTitleStore.getState().title).toBe("Read page title");
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(post).toHaveBeenCalledOnce();
    view.unmount();
  },
);

test.each(["content", "title", "restore", "readOnly", "navigation"] as const)(
  "drops a held generated title when %s changes during the lock",
  async (change) => {
    vi.useFakeTimers();
    let resolve!: (response: unknown) => void;
    post.mockImplementation(
      () =>
        new Promise((r) => {
          resolve = r;
        }),
    );
    useWorkflowTitleStore.getState().setTitle("New Agent");
    const task = { ...fixture().nodes[2]!, parentId: undefined };
    const view = renderHook(
      ({ nodes, readOnly }) =>
        useAutoGenerateWorkflowTitle(nodes, [], readOnly),
      { initialProps: { nodes: [task] as AppNode[], readOnly: false } },
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });
    const owner = { workflowPermanentId: "wpid_1", active: true };
    act(() => {
      registerEditorOwner(owner);
      expect(beginSaveTransaction(owner)).toBe(true);
    });
    await act(async () => {
      resolve({ data: { title: "Old content title" } });
    });
    expect(useWorkflowTitleStore.getState().title).toBe("New Agent");
    if (change === "content") {
      view.rerender({
        nodes: [
          {
            ...task,
            data: { ...task.data, url: "https://changed.test" },
          } as AppNode,
        ],
        readOnly: false,
      });
    } else if (change === "title") {
      act(() =>
        useWorkflowTitleStore
          .getState()
          .setTitle("Chosen title", { fromYamlCommit: true }),
      );
    } else if (change === "readOnly") {
      view.rerender({ nodes: [task], readOnly: true });
    } else if (change === "navigation") {
      view.unmount();
    }
    act(() => {
      finishSaveTransaction(owner);
      if (change === "restore")
        useWorkflowTitleStore.getState().restoreTitle("New Agent", false);
    });
    expect(useWorkflowTitleStore.getState().title).toBe(
      change === "title" ? "Chosen title" : "New Agent",
    );
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
    expect(post).toHaveBeenCalledOnce();
    if (change === "content") {
      post.mockResolvedValue({ data: { title: "Updated content title" } });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3999);
      });
      expect(post).toHaveBeenCalledOnce();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(post).toHaveBeenCalledTimes(2);
      expect(useWorkflowTitleStore.getState().title).toBe(
        "Updated content title",
      );
    }
    view.unmount();
  },
);

test("parks an isolated clean baseline and restores dirty flags without changing another workflow", () => {
  const baseline = {
    title: "Saved title",
    description: "Saved description",
    settings: getWorkflowSettings(fixture().nodes),
    blocks: [],
    parameters: [],
  };
  useWorkflowSnapshotStore.setState({
    snapshot: baseline,
    contentDirty: true,
    userHasEdited: true,
  });
  const parked = captureEditorState(fixture());
  baseline.settings.totpIdentifier = "Changed elsewhere";
  useWorkflowSnapshotStore.getState().clearSnapshot();
  expect(restoreEditorState(parked, deps("wpid_other"))).toBe(
    "refused-stale-workflow",
  );
  expect(useWorkflowSnapshotStore.getState()).toMatchObject({
    snapshot: null,
    contentDirty: false,
    userHasEdited: false,
  });
  expect(restoreEditorState(parked, deps())).toBe("restored");
  const restored = useWorkflowSnapshotStore.getState();
  expect(restored).toMatchObject({
    snapshot: { title: "Saved title" },
    contentDirty: true,
    userHasEdited: true,
  });
  expect(restored.snapshot!.settings.totpIdentifier).not.toBe(
    "Changed elsewhere",
  );
  restored.snapshot!.settings.totpIdentifier = "Changed after restore";
  expect(parked.workflowSnapshot!.snapshot!.settings.totpIdentifier).not.toBe(
    "Changed after restore",
  );
});

test("propagates a refused parameter restore without reporting success or replacing the graph", () => {
  const snapshot = captureEditorState(fixture());
  const target = deps();
  target.parametersStore = {
    ...target.parametersStore,
    setParameters: vi.fn(() => false),
  };
  expect(restoreEditorState(snapshot, target)).toBe("refused-locked");
  expect(target.setNodes).not.toHaveBeenCalled();
  expect(target.restoreOwnership).not.toHaveBeenCalled();
  expect(target.setEdges).not.toHaveBeenCalled();
  expect(target.scheduleLayout).not.toHaveBeenCalled();
});
