import type { ReactNode } from "react";
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { WorkflowScopeContext } from "../editor/WorkflowScopeContext";
import type { AppNode } from "../editor/nodes";
import {
  makeCollapseKey,
  useNodeCollapseStore,
} from "../editor/collapse/useNodeCollapseStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";

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
    useReactFlow: () => ({ setNodes: xyflow.setNodes }),
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
  xyflow.nodes = [makeNode("node-1", "Old_Label"), makeNode("node-2", "Peer")];
  xyflow.setNodes.mockReset();
  useNodeCollapseStore.setState({ collapsed: {} });
  useWorkflowParametersStore.setState({ parameters: [] });
  localStorage.clear();
});

describe("useNodeLabelChangeHandler collapse migration", () => {
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
