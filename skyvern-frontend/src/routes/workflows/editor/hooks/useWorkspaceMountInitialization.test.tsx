import { flushSync } from "react-dom";
import { QueryClient } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
} from "@testing-library/react";
import { type ReactNode, StrictMode, useLayoutEffect, useState } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import {
  clearDeferredEdits,
  deferredEdits,
  flushBufferedEditorEdits,
  useDeferredLockedEdit,
} from "@/hooks/useDeferredLockedEdit";
import { WorkflowPermanentIdContext } from "@/routes/workflows/WorkflowPermanentIdContext";
import { WorkflowScopeContext } from "../WorkflowScopeContext";
import { EditableNodeTitle } from "../nodes/components/EditableNodeTitle";
import { useDeferredTitleEdit } from "../../hooks/useDeferredTitleEdit";
import { useNodeLabelChangeHandler } from "../../hooks/useLabelChangeHandler";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import {
  beginSaveTransaction,
  createYamlCommitOwner,
  finishSaveTransaction,
  isEditorMutationLocked,
  registerEditorOwner,
  unregisterEditorOwner,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import type { AppNode } from "../nodes";
import { useSidebarStore } from "@/store/SidebarStore";

import {
  useWorkspaceDeferredEditCleanup,
  useWorkspaceMountInitialization,
} from "./useWorkspaceMountInitialization";

const flow = vi.hoisted(() => ({ nodes: [] as AppNode[] }));
vi.mock("@xyflow/react", async () => ({
  ...(await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react")),
  useReactFlow: () => ({
    getNodes: () => flow.nodes,
    setNodes: (nodes: AppNode[]) => {
      flow.nodes = nodes;
    },
  }),
}));

beforeEach(() => {
  clearDeferredEdits();
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
  useWorkflowHasChangesStore.setState(
    useWorkflowHasChangesStore.getInitialState(),
  );
  useWorkflowTitleStore.setState({ title: "Original" });
  useWorkflowParametersStore.setState({ parameters: [] });
  flow.nodes = [
    {
      id: "node",
      type: "task",
      position: { x: 0, y: 0 },
      data: { label: "Original" },
    } as AppNode,
  ];
});

afterEach(() => {
  cleanup();
  clearDeferredEdits();
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

function MountedWorkspace({ children }: { children: ReactNode }) {
  useWorkspaceMountInitialization({
    cacheKey: "default",
    closeWorkflowPanel: () => {},
    queryClient: new QueryClient(),
    workflowChangesStore: useWorkflowHasChangesStore.getState(),
    workflowPermanentId: "workflow-a",
  });
  return (
    <WorkflowPermanentIdContext.Provider value="workflow-a">
      <WorkflowScopeContext.Provider
        value={{ workflowId: "workflow-a", readOnly: false }}
      >
        {children}
      </WorkflowScopeContext.Provider>
    </WorkflowPermanentIdContext.Provider>
  );
}

function TitleEditor() {
  const { onTitleChange } = useDeferredTitleEdit();
  const title = useWorkflowTitleStore((state) => state.title);
  return <EditableNodeTitle value={title} editable onChange={onTitleChange} />;
}

function FieldEditor({
  savedValue,
  workflowId = "workflow-a",
}: {
  savedValue: string;
  workflowId?: string;
}) {
  const [value, setValue] = useState(savedValue);
  const edit = useDeferredLockedEdit({
    value,
    deferKey: JSON.stringify([workflowId, "node", "prompt"]),
    onChange: (next) => {
      setValue(next);
      useWorkflowHasChangesStore.getState().setHasChanges(true);
    },
  });
  return (
    <input
      aria-label="Prompt"
      value={edit.value}
      onChange={(event) => edit.onChange(event.target.value)}
    />
  );
}

function LabelEditor() {
  const [label, onChange] = useNodeLabelChangeHandler({
    id: "node",
    initialValue: String(flow.nodes[0]?.data.label),
  });
  return <EditableNodeTitle value={label} editable onChange={onChange} />;
}

describe.each([false, true])(
  "deferred edits restored before workspace initialization (strict mode: %s)",
  (strict) => {
    function mount(children: ReactNode) {
      const workspace = <MountedWorkspace>{children}</MountedWorkspace>;
      return render(strict ? <StrictMode>{workspace}</StrictMode> : workspace);
    }
    test.each(["title", "field", "label"])(
      "keeps a restored %s dirty after revisiting following a save",
      (kind) => {
        const editor = () =>
          kind === "title" ? (
            <TitleEditor />
          ) : kind === "label" ? (
            <LabelEditor />
          ) : (
            <FieldEditor savedValue="Original" />
          );
        const first = mount(editor());
        if (kind !== "field")
          fireEvent.click(screen.getByRole("heading", { name: "Original" }));
        fireEvent.change(screen.getByRole("textbox"), {
          target: { value: "Buffered" },
        });
        act(() =>
          useWorkflowYamlEditorStore.setState({
            commitInProgress: true,
            lockKind: "save",
          }),
        );
        first.unmount();
        act(() =>
          useWorkflowYamlEditorStore.setState({
            commitInProgress: false,
            lockKind: null,
          }),
        );
        // A successful save clears the departing editor before it is revisited.
        useWorkflowHasChangesStore.getState().setHasChanges(false);
        mount(editor());
        if (kind === "field")
          expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe(
            "Buffered",
          );
        else
          expect(
            screen.getByRole("heading", { name: "Buffered" }),
          ).toBeTruthy();
        if (kind === "label")
          expect(flow.nodes[0]?.data.label).toBe("Buffered");
        expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
      },
    );
    test.each(["title", "field", "label"])(
      "drops a retained %s when the stored value changed",
      (kind) => {
        const editor = (savedValue: string) =>
          kind === "title" ? (
            <TitleEditor />
          ) : kind === "label" ? (
            <LabelEditor />
          ) : (
            <FieldEditor savedValue={savedValue} />
          );
        const first = mount(editor("Original"));
        if (kind !== "field")
          fireEvent.click(screen.getByRole("heading", { name: "Original" }));
        fireEvent.change(screen.getByRole("textbox"), {
          target: { value: "Buffered" },
        });
        act(() =>
          useWorkflowYamlEditorStore.setState({
            commitInProgress: true,
            lockKind: "save",
          }),
        );
        first.unmount();
        act(() =>
          useWorkflowYamlEditorStore.setState({
            commitInProgress: false,
            lockKind: null,
          }),
        );
        useWorkflowTitleStore.getState().setTitle("Replaced");
        flow.nodes = flow.nodes.map((node) =>
          node.type === "task"
            ? { ...node, data: { ...node.data, label: "Replaced" } }
            : node,
        );
        useWorkflowHasChangesStore.getState().setHasChanges(true);
        mount(editor("Replaced"));
        if (kind === "field")
          expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe(
            "Replaced",
          );
        else
          expect(
            screen.getByRole("heading", { name: "Replaced" }),
          ).toBeTruthy();
        expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
      },
    );
  },
);

test("flushes only the editor callbacks registered when the flush starts", () => {
  vi.useFakeTimers();
  const commitNew = vi.fn();
  function Editor({
    name,
    onChange,
  }: {
    name: string;
    onChange: (value: string) => void;
  }) {
    const edit = useDeferredLockedEdit({ value: "", onChange });
    return (
      <input
        aria-label={name}
        value={edit.value}
        onChange={(event) => edit.onChange(event.target.value)}
      />
    );
  }
  const commitOriginal = vi.fn(() => {
    flushSync(() => {
      render(<Editor name="New editor" onChange={commitNew} />);
    });
    fireEvent.change(screen.getByRole("textbox", { name: "New editor" }), {
      target: { value: "new pending edit" },
    });
  });
  try {
    render(<Editor name="Original editor" onChange={commitOriginal} />);
    fireEvent.change(screen.getByRole("textbox", { name: "Original editor" }), {
      target: { value: "original pending edit" },
    });
    act(() => flushBufferedEditorEdits());
    expect(commitOriginal).toHaveBeenCalledWith("original pending edit");
    expect(commitNew).not.toHaveBeenCalled();
    act(() => flushBufferedEditorEdits());
    expect(commitNew).toHaveBeenCalledExactlyOnceWith("new pending edit");
  } finally {
    cleanup();
    vi.useRealTimers();
  }
});

test.each(["label", "field"])(
  "discard clears an unscoped deferred %s without clearing another workflow",
  (kind) => {
    const { result } = renderHook(() => {
      const [, labelChange] = useNodeLabelChangeHandler({
        id: "node",
        initialValue: "Original",
      });
      const field = useDeferredLockedEdit({
        value: "Original",
        deferKey: JSON.stringify([null, "node", "prompt"]),
        onChange: (value) => {
          flow.nodes[0]!.data.label = value;
        },
      });
      return kind === "label" ? labelChange : field.onChange;
    });
    if (kind === "label")
      act(() =>
        useWorkflowYamlEditorStore.setState({
          commitInProgress: true,
          lockKind: "save",
        }),
      );
    act(() => result.current("Discard me"));
    if (kind === "field")
      act(() =>
        useWorkflowYamlEditorStore.setState({
          commitInProgress: true,
          lockKind: "save",
        }),
      );
    const otherKey = JSON.stringify(["workflow-b", "node", "prompt"]);
    deferredEdits.set(otherKey, {
      value: "Keep me",
      propValue: "Original",
      propChanged: false,
    });
    expect(deferredEdits.size).toBe(2);
    clearDeferredEdits("workflow-a");
    expect([...deferredEdits.keys()]).toEqual([otherKey]);
    act(() =>
      useWorkflowYamlEditorStore.setState({
        commitInProgress: false,
        lockKind: null,
      }),
    );
    expect(flow.nodes[0]!.data.label).toBe("Original");
  },
);

test.each(["title", "field", "label"])(
  "a replay for workflow A does not mark workflow B dirty during mount (%s)",
  (kind) => {
    const workflowBChanges = { setHasChanges: vi.fn() };
    function WorkflowB() {
      useWorkspaceMountInitialization({
        cacheKey: "default",
        closeWorkflowPanel: () => {},
        queryClient: new QueryClient(),
        workflowChangesStore: workflowBChanges,
        workflowPermanentId: "workflow-b",
      });
      return (
        <WorkflowPermanentIdContext.Provider value="workflow-a">
          <WorkflowScopeContext.Provider
            value={{ workflowId: "workflow-a", readOnly: false }}
          >
            {kind === "title" ? (
              <TitleEditor />
            ) : kind === "label" ? (
              <LabelEditor />
            ) : (
              <FieldEditor savedValue="Original" />
            )}
          </WorkflowScopeContext.Provider>
        </WorkflowPermanentIdContext.Provider>
      );
    }
    deferredEdits.set(
      kind === "title"
        ? "workflow-a:title"
        : JSON.stringify([
            "workflow-a",
            "node",
            kind === "label" ? "label" : "prompt",
          ]),
      {
        value: "Buffered",
        propValue: "Original",
        propChanged: false,
        workflowId: "workflow-a",
      },
    );
    render(<WorkflowB />);
    expect(workflowBChanges.setHasChanges).toHaveBeenLastCalledWith(false);
    if (kind === "field")
      expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe(
        "Buffered",
      );
    else expect(screen.getByRole("heading", { name: "Buffered" })).toBeTruthy();
  },
);

test.each(["title", "label", "field"])(
  "preserves an outgoing workflow's pending-save %s buffer across in-place navigation",
  (kind) => {
    function Session({ workflowId }: { workflowId: string }) {
      useWorkspaceDeferredEditCleanup(workflowId);
      useLayoutEffect(() => {
        const owner = createYamlCommitOwner(workflowId);
        registerEditorOwner(owner);
        return () => unregisterEditorOwner(owner);
      }, [workflowId]);
      return (
        <WorkflowPermanentIdContext.Provider value={workflowId}>
          <WorkflowScopeContext.Provider
            value={{ workflowId, readOnly: false }}
          >
            {kind === "title" ? (
              <TitleEditor key={workflowId} />
            ) : kind === "label" ? (
              <LabelEditor key={workflowId} />
            ) : (
              <FieldEditor
                key={workflowId}
                workflowId={workflowId}
                savedValue="Original"
              />
            )}
          </WorkflowScopeContext.Provider>
        </WorkflowPermanentIdContext.Provider>
      );
    }
    const view = render(<Session workflowId="workflow-a" />);
    if (kind !== "field")
      fireEvent.click(screen.getByRole("heading", { name: "Original" }));
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "Buffered" },
    });
    const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
    act(() => expect(beginSaveTransaction(owner)).toBe(true));
    const key =
      kind === "title"
        ? "workflow-a:title"
        : JSON.stringify([
            "workflow-a",
            "node",
            kind === "label" ? "label" : "prompt",
          ]);
    expect(deferredEdits.get(key)?.value).toBe("Buffered");
    view.rerender(<Session workflowId="workflow-b" />);
    expect(isEditorMutationLocked()).toBe(false);
    expect(
      useWorkflowYamlEditorStore.getState().pendingSaves["workflow-a"]?.owner,
    ).toBe(owner);
    expect(deferredEdits.get(key)?.value).toBe("Buffered");
    act(() => finishSaveTransaction(owner));
    view.rerender(<Session workflowId="workflow-a" />);
    if (kind === "field")
      expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe(
        "Buffered",
      );
    else expect(screen.getByRole("heading", { name: "Buffered" })).toBeTruthy();
    expect(deferredEdits.has(key)).toBe(false);
    if (kind !== "field")
      expect(
        kind === "title"
          ? useWorkflowTitleStore.getState().title
          : flow.nodes[0]?.data.label,
      ).toBe("Buffered");
  },
);

test("an unrelated workflow's save does not retain the outgoing workflow's discarded buffer", () => {
  const { rerender } = renderHook(
    ({ workflowId }) => useWorkspaceDeferredEditCleanup(workflowId),
    { initialProps: { workflowId: "workflow-a" } },
  );
  deferredEdits.set("workflow-a:title", {
    value: "Discarded",
    propValue: "Original",
    propChanged: false,
  });
  const other = createYamlCommitOwner("workflow-b");
  act(() => {
    registerEditorOwner(other);
    expect(beginSaveTransaction(other)).toBe(true);
  });
  rerender({ workflowId: "workflow-b" });
  expect(deferredEdits.has("workflow-a:title")).toBe(false);
  act(() => finishSaveTransaction(other));
});
