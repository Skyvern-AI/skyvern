import { render, waitFor } from "@testing-library/react";
import {
  ReactFlow,
  ReactFlowProvider,
  useNodesState,
  useReactFlow,
  type NodeChange,
} from "@xyflow/react";
import { useEffect } from "react";
import { beforeAll, beforeEach, expect, test } from "vitest";

import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";

import type { AppNode } from "./nodes";
import { loopNodeDefaultData } from "./nodes/LoopNode/types";
import { changesIncludeWorkflowEdit } from "./workflowEditChanges";

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

let observed: Array<NodeChange<AppNode>> = [];

beforeEach(() => {
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
  useWorkflowHasChangesStore.setState({ hasChanges: false });
  observed = [];
});

function makeNodes(): Array<AppNode> {
  return [
    {
      id: "loop-alpha",
      type: "loop",
      position: { x: 0, y: 0 },
      data: { ...loopNodeDefaultData, label: "loop_alpha", _headerHeight: 180 },
    },
  ];
}

// Mirrors the editor's wiring: Workspace owns the node state and FlowRenderer
// marks the workflow dirty from the changes React Flow reports — the flag that
// gates the "unsaved changes" navigation prompt.
function Harness({ write }: { write: Record<string, unknown> }) {
  const [nodes, , applyChanges] = useNodesState<AppNode>(makeNodes());
  const { updateNodeData } = useReactFlow();
  useEffect(() => {
    updateNodeData("loop-alpha", write);
  }, [write, updateNodeData]);
  return (
    <ReactFlow
      nodes={nodes}
      edges={[]}
      onNodesChange={(changes) => {
        observed.push(...changes);
        if (changesIncludeWorkflowEdit(changes, nodes)) {
          useWorkflowHasChangesStore.getState().setHasChanges(true);
        }
        applyChanges(changes);
      }}
    />
  );
}

function renderHarness(write: Record<string, unknown>) {
  render(
    <ReactFlowProvider>
      <Harness write={write} />
    </ReactFlowProvider>,
  );
}

function sawDataWrite() {
  return observed.some(
    (change) => change.type === "replace" && change.id === "loop-alpha",
  );
}

// A loop/conditional header writes its measured height back into node data, so
// it reaches onNodesChange as a `replace` exactly like a real edit. Closing the
// studio's editor pane re-measures every header (SKY-13875).
test("a header measurement does not mark the workflow dirty", async () => {
  renderHarness({ _headerHeight: 240 });

  // Wait for the write to actually reach React Flow so the assertion below
  // can't pass just because nothing happened yet.
  await waitFor(() => expect(sawDataWrite()).toBe(true));
  expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
});

test("editing block data does mark the workflow dirty", async () => {
  renderHarness({ loopValue: "renamed_items" });

  await waitFor(() =>
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true),
  );
});

// A measurement riding along with a real edit must still mark the workflow
// dirty — the edit is what the save prompt exists for.
test("a header measurement bundled with an edit marks the workflow dirty", async () => {
  renderHarness({ _headerHeight: 240, loopValue: "renamed_items" });

  await waitFor(() =>
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true),
  );
});
