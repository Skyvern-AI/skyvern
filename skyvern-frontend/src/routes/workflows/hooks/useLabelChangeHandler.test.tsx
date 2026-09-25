import type { ReactNode } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { WorkflowScopeContext } from "../editor/WorkflowScopeContext";
import type { AppNode } from "../editor/nodes";
import {
  makeCollapseKey,
  useNodeCollapseStore,
} from "../editor/collapse/useNodeCollapseStore";
import {
  beginCopilotAcceptance,
  finishCopilotAcceptance,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import {
  clearDeferredEdits,
  deferredEdits,
} from "@/hooks/useDeferredLockedEdit";

import { EditableNodeTitle } from "../editor/nodes/components/EditableNodeTitle";

import { useNodeLabelChangeHandler } from "./useLabelChangeHandler";

const xyflow = vi.hoisted(() => ({
  nodes: [] as unknown[],
  setNodes: vi.fn(),
}));

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useNodes: () => xyflow.nodes,
    useReactFlow: () => ({
      getNodes: () => xyflow.nodes,
      setNodes: xyflow.setNodes,
    }),
  };
});

function makeNode(id: string, label: string): AppNode {
  return {
    id,
    type: "task",
    position: { x: 0, y: 0 },
    data: { label },
  } as AppNode;
}

function wrapper({ children }: { children: ReactNode }) {
  return (
    <WorkflowScopeContext.Provider
      value={{ workflowId: "wf-rename", readOnly: false }}
    >
      {children}
    </WorkflowScopeContext.Provider>
  );
}

beforeEach(() => {
  clearDeferredEdits();
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
  xyflow.nodes = [makeNode("node-1", "Old_Label"), makeNode("node-2", "Peer")];
  xyflow.setNodes.mockReset().mockImplementation((nodes: AppNode[]) => {
    xyflow.nodes = nodes;
  });
  useNodeCollapseStore.setState({ collapsed: {} });
  useWorkflowParametersStore.setState({ parameters: [] });
  localStorage.clear();
});

afterEach(cleanup);

function EditableLabel() {
  const [label, onChange] = useNodeLabelChangeHandler({
    id: "node-1",
    initialValue: "Old_Label",
  });
  return <EditableNodeTitle value={label} editable onChange={onChange} />;
}

describe("useNodeLabelChangeHandler collapse migration", () => {
  test("holds a reserved rename before local label or collapse changes", () => {
    useNodeCollapseStore.getState().toggleBlock("wf-rename", "Old_Label");
    const { result } = renderHook(
      () =>
        useNodeLabelChangeHandler({ id: "node-1", initialValue: "Old_Label" }),
      { wrapper },
    );
    let token: symbol;
    act(() => {
      token = beginCopilotAcceptance()!;
    });
    act(() => result.current[1]("New_Label"));
    expect(result.current[0]).toBe("Old_Label");
    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-rename", "Old_Label")
      ],
    ).toBe(true);
    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-rename", "New_Label")
      ],
    ).toBeUndefined();
    expect(xyflow.setNodes).not.toHaveBeenCalled();
    act(() => finishCopilotAcceptance(token));
    expect(xyflow.setNodes).toHaveBeenCalledTimes(1);
    expect(result.current[0]).toBe("New_Label");
  });
  test("renaming a collapsed block migrates persisted collapse state", () => {
    act(() => {
      useNodeCollapseStore.getState().toggleBlock("wf-rename", "Old_Label");
    });

    const { result } = renderHook(
      () =>
        useNodeLabelChangeHandler({ id: "node-1", initialValue: "Old_Label" }),
      { wrapper },
    );

    act(() => {
      result.current[1]("New_Label");
    });

    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-rename", "Old_Label")
      ],
    ).toBeUndefined();
    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-rename", "New_Label")
      ],
    ).toBe(true);
    expect(xyflow.setNodes).toHaveBeenCalled();
  });
});

describe("deferred block label edits", () => {
  test("preserves a parked label when another workflow mounts the same node id", () => {
    const first = renderHook(
      () =>
        useNodeLabelChangeHandler({ id: "node-1", initialValue: "Old_Label" }),
      { wrapper },
    );
    let token: symbol;
    act(() => {
      token = beginCopilotAcceptance()!;
    });
    act(() => first.result.current[1]("New_Label"));
    first.unmount();
    act(() => finishCopilotAcceptance(token));
    const parkedEntries = [...deferredEdits.entries()];
    expect(parkedEntries).toHaveLength(1);

    xyflow.nodes = [makeNode("node-1", "Other_Label")];
    const other = renderHook(
      () =>
        useNodeLabelChangeHandler({
          id: "node-1",
          initialValue: "Other_Label",
        }),
      {
        wrapper: ({ children }: { children: ReactNode }) => (
          <WorkflowScopeContext.Provider
            value={{ workflowId: "wf-other", readOnly: false }}
          >
            {children}
          </WorkflowScopeContext.Provider>
        ),
      },
    );
    expect(other.result.current[0]).toBe("Other_Label");
    expect(xyflow.setNodes).not.toHaveBeenCalled();
    expect([...deferredEdits.entries()]).toEqual(parkedEntries);
    other.unmount();

    xyflow.nodes = [makeNode("node-1", "Old_Label")];
    const resumed = renderHook(
      () =>
        useNodeLabelChangeHandler({ id: "node-1", initialValue: "Old_Label" }),
      { wrapper },
    );
    expect(resumed.result.current[0]).toBe("New_Label");
    expect((xyflow.nodes[0] as AppNode).data.label).toBe("New_Label");
    expect(xyflow.setNodes).toHaveBeenCalledTimes(1);
    expect(deferredEdits.size).toBe(0);
  });

  test("sweeps a parked draft for a node that never remounts", () => {
    const first = renderHook(
      () =>
        useNodeLabelChangeHandler({ id: "node-1", initialValue: "Old_Label" }),
      { wrapper },
    );
    let token: symbol;
    act(() => {
      token = beginCopilotAcceptance()!;
    });
    act(() => first.result.current[1]("New_Label"));
    first.unmount();
    expect(
      deferredEdits.get(JSON.stringify(["wf-rename", "node-1", "label"]))
        ?.value,
    ).toBe("New_Label");

    clearDeferredEdits();

    expect(deferredEdits.size).toBe(0);
    act(() => finishCopilotAcceptance(token));
    renderHook(
      () =>
        useNodeLabelChangeHandler({ id: "node-1", initialValue: "Old_Label" }),
      { wrapper },
    );
    expect(xyflow.setNodes).not.toHaveBeenCalled();
  });

  test("applies a held label once after the handler unmounts and remounts", () => {
    const first = renderHook(
      () =>
        useNodeLabelChangeHandler({ id: "node-1", initialValue: "Old_Label" }),
      { wrapper },
    );
    let token: symbol;
    act(() => {
      token = beginCopilotAcceptance()!;
    });
    act(() => first.result.current[1]("New_Label"));
    expect(xyflow.setNodes).not.toHaveBeenCalled();
    first.unmount();

    const second = renderHook(
      () =>
        useNodeLabelChangeHandler({ id: "node-1", initialValue: "Old_Label" }),
      { wrapper },
    );
    expect(xyflow.setNodes).not.toHaveBeenCalled();
    act(() => finishCopilotAcceptance(token));
    expect(second.result.current[0]).toBe("New_Label");
    expect((xyflow.nodes[0] as AppNode).data.label).toBe("New_Label");
    expect(xyflow.setNodes).toHaveBeenCalledTimes(1);
    second.unmount();
    renderHook(
      () =>
        useNodeLabelChangeHandler({ id: "node-1", initialValue: "New_Label" }),
      { wrapper },
    );
    expect(xyflow.setNodes).toHaveBeenCalledTimes(1);
  });

  test.each(["save", "copilot"] as const)(
    "applies an edit once after a %s lock captures it and the input blurs",
    (kind) => {
      render(<EditableLabel />, { wrapper });
      fireEvent.click(screen.getByRole("heading", { name: "Old_Label" }));
      const input = screen.getByRole("textbox");
      fireEvent.change(input, { target: { value: "New_Label" } });
      let token: symbol;
      act(() => {
        if (kind === "copilot") token = beginCopilotAcceptance()!;
        else
          useWorkflowYamlEditorStore.setState({
            commitInProgress: true,
            lockKind: "save",
          });
        fireEvent.blur(input);
      });
      expect(xyflow.setNodes).not.toHaveBeenCalled();
      expect((xyflow.nodes[0] as AppNode).data.label).toBe("Old_Label");
      act(() => {
        if (kind === "copilot") finishCopilotAcceptance(token);
        else
          useWorkflowYamlEditorStore.setState({
            commitInProgress: false,
            lockKind: null,
          });
      });
      expect((xyflow.nodes[0] as AppNode).data.label).toBe("New_Label");
      expect(xyflow.setNodes).toHaveBeenCalledTimes(1);
    },
  );

  test("captures an active draft when locking without waiting for blur", () => {
    render(<EditableLabel />, { wrapper });
    fireEvent.click(screen.getByRole("heading", { name: "Old_Label" }));
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "New_Label" },
    });
    let token: symbol;
    act(() => {
      token = beginCopilotAcceptance()!;
    });
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(xyflow.setNodes).not.toHaveBeenCalled();
    act(() => finishCopilotAcceptance(token));
    expect((xyflow.nodes[0] as AppNode).data.label).toBe("New_Label");
    expect(xyflow.setNodes).toHaveBeenCalledTimes(1);
  });

  test("cannot begin editing while locked", () => {
    act(() => {
      beginCopilotAcceptance();
    });
    render(<EditableLabel />, { wrapper });
    fireEvent.click(screen.getByRole("heading", { name: "Old_Label" }));
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  test.each(["renamed", "removed"])(
    "drops the held edit if the node was %s",
    (change) => {
      const { result } = renderHook(
        () =>
          useNodeLabelChangeHandler({
            id: "node-1",
            initialValue: "Old_Label",
          }),
        { wrapper },
      );
      let token: symbol;
      act(() => {
        token = beginCopilotAcceptance()!;
      });
      act(() => result.current[1]("New_Label"));
      xyflow.nodes =
        change === "renamed" ? [makeNode("node-1", "Copilot_Label")] : [];
      act(() => finishCopilotAcceptance(token));
      expect(xyflow.setNodes).not.toHaveBeenCalled();
    },
  );
});
