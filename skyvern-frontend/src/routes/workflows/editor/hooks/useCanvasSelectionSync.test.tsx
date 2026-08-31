import { render, waitFor } from "@testing-library/react";
import {
  ReactFlow,
  ReactFlowProvider,
  useNodesState,
  useReactFlow,
  useStoreApi,
  type Node,
  type NodeChange,
} from "@xyflow/react";
import { useEffect } from "react";
import { beforeAll, beforeEach, expect, test } from "vitest";

import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";

import { hasStructuralNodeChange } from "../structuralNodeChanges";
import { useCanvasSelectionSync } from "./useCanvasSelectionSync";

// React Flow measures nodes through APIs jsdom doesn't implement.
beforeAll(() => {
  (globalThis as { ResizeObserver?: unknown }).ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  (globalThis as { DOMMatrixReadOnly?: unknown }).DOMMatrixReadOnly = class {
    m22 = 1;
  };
  Object.defineProperties(HTMLElement.prototype, {
    offsetHeight: { get: () => 100 },
    offsetWidth: { get: () => 100 },
  });
});

function makeNodes(): Array<Node> {
  return [
    { id: "block-alpha", position: { x: 0, y: 0 }, data: {} },
    { id: "block-beta", position: { x: 0, y: 100 }, data: {} },
  ];
}

let storeApi: ReturnType<typeof useStoreApi> | null = null;
let observed: Array<NodeChange> = [];

beforeEach(() => {
  storeApi = null;
  observed = [];
  useWorkflowHasChangesStore.setState({ hasChanges: false });
});

function selectedNodeIds(): Array<string> {
  return [...(storeApi?.getState().nodeLookup.values() ?? [])]
    .filter((node) => node.selected)
    .map((node) => node.id);
}

function Canvas({
  selectedBlockId,
  edit,
}: {
  selectedBlockId: string | null;
  edit: boolean;
}) {
  storeApi = useStoreApi();
  useCanvasSelectionSync(selectedBlockId);
  const { updateNodeData } = useReactFlow();
  useEffect(() => {
    if (edit) {
      updateNodeData("block-alpha", { label: "renamed" });
    }
  }, [edit, updateNodeData]);
  return null;
}

// Mirrors the editor's wiring: Workspace owns the node state via
// `useNodesState` and FlowRenderer marks the workflow dirty on a structural
// change — the flag that gates the "unsaved changes" navigation prompt.
function Harness({
  selectedBlockId,
  edit = false,
}: {
  selectedBlockId: string | null;
  edit?: boolean;
}) {
  const [nodes, , applyChanges] = useNodesState(makeNodes());
  return (
    <ReactFlow
      nodes={nodes}
      edges={[]}
      onNodesChange={(changes) => {
        observed.push(...changes);
        if (hasStructuralNodeChange(changes)) {
          useWorkflowHasChangesStore.getState().setHasChanges(true);
        }
        applyChanges(changes);
      }}
    >
      <Canvas selectedBlockId={selectedBlockId} edit={edit} />
    </ReactFlow>
  );
}

function tree(props: { selectedBlockId: string | null; edit?: boolean }) {
  return (
    <ReactFlowProvider>
      <Harness {...props} />
    </ReactFlowProvider>
  );
}

test("selecting a block does not mark the workflow dirty", async () => {
  const { rerender } = render(tree({ selectedBlockId: null }));
  rerender(tree({ selectedBlockId: "block-beta" }));

  // Wait for the selection to actually reach React Flow — by whatever change
  // type it is written as — so the assertion below can't pass just because
  // nothing happened yet.
  await waitFor(() =>
    expect(observed.some((c) => "id" in c && c.id === "block-beta")).toBe(true),
  );
  expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
});

test("editing a block does mark the workflow dirty", async () => {
  render(tree({ selectedBlockId: "block-beta", edit: true }));

  await waitFor(() =>
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true),
  );
});

// The hook's contract is lockstep with the single selected-block store, so the
// canvas must never keep a stale block selected — including while React Flow's
// multi-select modifier is held, where `addSelectedNodes` alone only adds.
test("keeps exactly one block selected while multi-select is active", async () => {
  const { rerender } = render(tree({ selectedBlockId: "block-alpha" }));
  await waitFor(() => expect(selectedNodeIds()).toEqual(["block-alpha"]));

  storeApi?.setState({ multiSelectionActive: true });
  rerender(tree({ selectedBlockId: "block-beta" }));

  await waitFor(() => expect(selectedNodeIds()).toEqual(["block-beta"]));
});
