import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type { NodeProps } from "@xyflow/react";
import {
  beginSaveTransaction,
  createYamlCommitOwner,
  finishSaveTransaction,
  registerEditorOwner,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import { WorkflowPermanentIdContext } from "../../WorkflowPermanentIdContext";
import { focusBlockTarget } from "../../studio/blockSearch";
import { useWorkflowGraphState } from "../workflowEditorUtils";
import type { AppNode } from "../nodes";
import { LoopNode } from "../nodes/LoopNode/LoopNode";
import { loopNodeDefaultData } from "../nodes/LoopNode/types";
import { ConditionalNode } from "../nodes/ConditionalNode/ConditionalNode";
import { conditionalNodeDefaultData } from "../nodes/ConditionalNode/types";
import { codeBlockNodeDefaultData } from "../nodes/CodeBlockNode/types";
import { WorkflowScopeContext } from "../WorkflowScopeContext";

import {
  makeCollapseKey,
  useIsBlockCollapsed,
  useNodeCollapseStore,
} from "./useNodeCollapseStore";

let graph: ReturnType<typeof useWorkflowGraphState>;
const updateNodeData = vi.fn();

vi.mock("@xyflow/react", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@xyflow/react")>()),
  Handle: () => null,
  useNodes: () => graph.nodes,
  useReactFlow: () => ({ setNodes: graph.setNodes, updateNodeData }),
}));
vi.mock("../nodes/components/NodeHeader", () => ({ NodeHeader: () => null }));
vi.mock("../nodes/LoopNode/LoopEditor", () => ({ LoopEditor: () => null }));
vi.mock("../nodes/ConditionalNode/BranchesEditor", () => ({
  BranchesEditor: () => null,
}));
vi.mock("../nodes/BuildModeOnly", () => ({ BuildModeOnly: () => null }));
vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: undefined }),
}));

function ContainerGraph({ initialNodes }: { initialNodes: AppNode[] }) {
  graph = useWorkflowGraphState(initialNodes, []);
  const container = graph.nodes[0]!;
  const props = {
    ...container,
    selected: false,
    dragging: false,
    draggable: false,
    selectable: true,
    deletable: false,
    isConnectable: false,
    zIndex: 0,
    positionAbsoluteX: 0,
    positionAbsoluteY: 0,
  };
  return (
    <WorkflowPermanentIdContext.Provider value={WF}>
      <WorkflowScopeContext.Provider
        value={{ workflowId: WF, readOnly: false }}
      >
        {container.type === "loop" ? (
          <LoopNode
            {...(props as NodeProps<Extract<AppNode, { type?: "loop" }>>)}
          />
        ) : (
          <ConditionalNode
            {...(props as NodeProps<
              Extract<AppNode, { type?: "conditional" }>
            >)}
          />
        )}
      </WorkflowScopeContext.Provider>
    </WorkflowPermanentIdContext.Provider>
  );
}

function resetStore() {
  useNodeCollapseStore.setState({ collapsed: {} });
}

beforeEach(() => {
  resetStore();
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
  vi.unstubAllGlobals();
  localStorage.clear();
});

const WF = "wf_test";

describe("useNodeCollapseStore (SKY-9069 / SKY-9361)", () => {
  test("initial state has no collapsed blocks", () => {
    expect(useNodeCollapseStore.getState().collapsed).toEqual({});
  });

  test("unknown labels read as not-collapsed by default", () => {
    const store = useNodeCollapseStore.getState();
    expect(Boolean(store.collapsed[`${WF}\x1funknown`])).toBe(false);
  });

  test("toggleBlock flips a label from expanded to collapsed and back", () => {
    const { toggleBlock } = useNodeCollapseStore.getState();
    toggleBlock(WF, "alpha");
    expect(useNodeCollapseStore.getState().collapsed[`${WF}\x1falpha`]).toBe(
      true,
    );
    toggleBlock(WF, "alpha");
    expect(`${WF}\x1falpha` in useNodeCollapseStore.getState().collapsed).toBe(
      false,
    );
  });

  test("toggleBlock is independent across labels", () => {
    const { toggleBlock } = useNodeCollapseStore.getState();
    toggleBlock(WF, "alpha");
    toggleBlock(WF, "beta");
    expect(useNodeCollapseStore.getState().collapsed).toEqual({
      [`${WF}\x1falpha`]: true,
      [`${WF}\x1fbeta`]: true,
    });
    toggleBlock(WF, "alpha");
    expect(useNodeCollapseStore.getState().collapsed).toEqual({
      [`${WF}\x1fbeta`]: true,
    });
  });

  test("collapseAll sets every passed label in the workflow to true", () => {
    const { collapseAll } = useNodeCollapseStore.getState();
    collapseAll(WF, ["a", "b", "c"]);
    expect(useNodeCollapseStore.getState().collapsed).toEqual({
      [`${WF}\x1fa`]: true,
      [`${WF}\x1fb`]: true,
      [`${WF}\x1fc`]: true,
    });
  });

  test("collapseAll preserves entries from other workflows", () => {
    useNodeCollapseStore.setState({
      collapsed: {
        "wf_other\x1fkeep": true,
        [`${WF}\x1fexisting`]: false,
      },
    });
    const { collapseAll } = useNodeCollapseStore.getState();
    collapseAll(WF, ["existing", "fresh"]);
    expect(useNodeCollapseStore.getState().collapsed).toEqual({
      "wf_other\x1fkeep": true,
      [`${WF}\x1fexisting`]: true,
      [`${WF}\x1ffresh`]: true,
    });
  });

  test("expandAll clears entries only within the given workflow", () => {
    const { collapseAll, expandAll } = useNodeCollapseStore.getState();
    collapseAll("wf_other", ["x"]);
    collapseAll(WF, ["a", "b"]);
    expandAll(WF);
    expect(useNodeCollapseStore.getState().collapsed).toEqual({
      "wf_other\x1fx": true,
    });
  });

  test("expandBlock clears a collapsed label", () => {
    const { toggleBlock, expandBlock } = useNodeCollapseStore.getState();
    toggleBlock(WF, "alpha");
    expect(useNodeCollapseStore.getState().collapsed[`${WF}\x1falpha`]).toBe(
      true,
    );
    expandBlock(WF, "alpha");
    expect(`${WF}\x1falpha` in useNodeCollapseStore.getState().collapsed).toBe(
      false,
    );
  });

  test("expandBlock is a no-op for an already-open block", () => {
    const before = useNodeCollapseStore.getState().collapsed;
    useNodeCollapseStore.getState().expandBlock(WF, "never-collapsed");
    expect(useNodeCollapseStore.getState().collapsed).toBe(before);
  });

  test("expandBlock leaves other labels and workflows untouched", () => {
    const { collapseAll, expandBlock } = useNodeCollapseStore.getState();
    collapseAll("wf_other", ["x"]);
    collapseAll(WF, ["a", "b"]);
    expandBlock(WF, "a");
    expect(useNodeCollapseStore.getState().collapsed).toEqual({
      "wf_other\x1fx": true,
      [`${WF}\x1fb`]: true,
    });
  });

  test("pruneStaleLabels drops entries whose labels are no longer present", () => {
    const { collapseAll, pruneStaleLabels } = useNodeCollapseStore.getState();
    collapseAll("wf_other", ["keep"]);
    collapseAll(WF, ["alpha", "beta", "gamma"]);
    pruneStaleLabels(WF, ["alpha"]);
    expect(useNodeCollapseStore.getState().collapsed).toEqual({
      "wf_other\x1fkeep": true,
      [`${WF}\x1falpha`]: true,
    });
  });

  test("pruneStaleLabels preserves entries in other workflows", () => {
    const { collapseAll, pruneStaleLabels } = useNodeCollapseStore.getState();
    collapseAll("wf_other", ["x", "y"]);
    collapseAll(WF, ["a"]);
    pruneStaleLabels(WF, []);
    expect(useNodeCollapseStore.getState().collapsed).toEqual({
      "wf_other\x1fx": true,
      "wf_other\x1fy": true,
    });
  });
});

function Probe({ label }: { label: string }) {
  const collapsed = useIsBlockCollapsed(label);
  return <div data-testid="state">{collapsed ? "1" : "0"}</div>;
}

describe("useNodeCollapseStore - workflow scoping", () => {
  test("collapsed state is namespaced by workflow id", () => {
    const { rerender } = render(
      <WorkflowScopeContext.Provider
        value={{ workflowId: "wf_a", readOnly: false }}
      >
        <Probe label="step1" />
      </WorkflowScopeContext.Provider>,
    );
    act(() => {
      useNodeCollapseStore.getState().toggleBlock("wf_a", "step1");
    });
    expect(screen.getByTestId("state").textContent).toBe("1");

    rerender(
      <WorkflowScopeContext.Provider
        value={{ workflowId: "wf_b", readOnly: false }}
      >
        <Probe label="step1" />
      </WorkflowScopeContext.Provider>,
    );
    expect(screen.getByTestId("state").textContent).toBe("0");
  });

  test("falls back to __global__ scope when no provider", () => {
    render(<Probe label="step1" />);
    act(() => {
      useNodeCollapseStore.getState().toggleBlock("__global__", "step1");
    });
    expect(screen.getByTestId("state").textContent).toBe("1");
  });

  test("read-only scope ignores persisted collapse state", () => {
    act(() => {
      useNodeCollapseStore.getState().toggleBlock("wf_a", "step1");
    });
    const { rerender } = render(
      <WorkflowScopeContext.Provider
        value={{ workflowId: "wf_a", readOnly: false }}
      >
        <Probe label="step1" />
      </WorkflowScopeContext.Provider>,
    );
    expect(screen.getByTestId("state").textContent).toBe("1");

    rerender(
      <WorkflowScopeContext.Provider
        value={{ workflowId: "wf_a", readOnly: true }}
      >
        <Probe label="step1" />
      </WorkflowScopeContext.Provider>,
    );
    expect(screen.getByTestId("state").textContent).toBe("0");
  });
});

describe("useNodeCollapseStore - persistence", () => {
  test("persists to localStorage under skyvern:node-collapse", () => {
    act(() => {
      useNodeCollapseStore.getState().toggleBlock("wf_x", "stepA");
    });
    const raw = localStorage.getItem("skyvern:node-collapse");
    expect(raw).not.toBeNull();
    const parsed = JSON.parse(raw!);
    expect(parsed.state.collapsed["wf_x\x1fstepA"]).toBe(true);
  });
});

describe("renameBlock", () => {
  test("moves the collapsed entry from oldLabel to newLabel within the same workflow", () => {
    const store = useNodeCollapseStore.getState();
    act(() => {
      store.toggleBlock("wf-1", "Old Label");
    });
    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-1", "Old Label")
      ],
    ).toBe(true);

    act(() => {
      useNodeCollapseStore
        .getState()
        .renameBlock("wf-1", "Old Label", "New Label");
    });

    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-1", "Old Label")
      ],
    ).toBeUndefined();
    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-1", "New Label")
      ],
    ).toBe(true);
  });

  test("no-ops when oldLabel has no entry (block was open)", () => {
    act(() => {
      useNodeCollapseStore.getState().renameBlock("wf-1", "Missing", "Renamed");
    });

    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-1", "Renamed")
      ],
    ).toBeUndefined();
    expect(Object.keys(useNodeCollapseStore.getState().collapsed)).toHaveLength(
      0,
    );
  });

  test("does not cross workflow boundaries", () => {
    act(() => {
      useNodeCollapseStore.getState().toggleBlock("wf-1", "Shared Label");
      useNodeCollapseStore.getState().toggleBlock("wf-2", "Shared Label");
    });
    act(() => {
      useNodeCollapseStore
        .getState()
        .renameBlock("wf-1", "Shared Label", "Renamed");
    });

    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-1", "Shared Label")
      ],
    ).toBeUndefined();
    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-1", "Renamed")
      ],
    ).toBe(true);
    // wf-2 entry untouched.
    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-2", "Shared Label")
      ],
    ).toBe(true);
    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-2", "Renamed")
      ],
    ).toBeUndefined();
  });
});

test.each(["loop", "conditional"] as const)(
  "%s reveals descendants after search expands it during a save",
  async (type) => {
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        disconnect() {}
      },
    );
    const container: AppNode =
      type === "loop"
        ? {
            id: "container",
            type,
            position: { x: 0, y: 0 },
            data: { ...loopNodeDefaultData, label: "container" },
          }
        : {
            id: "container",
            type,
            position: { x: 0, y: 0 },
            data: { ...conditionalNodeDefaultData, label: "container" },
          };
    const child: AppNode = {
      id: "child",
      type: "codeBlock",
      parentId: container.id,
      position: { x: 0, y: 0 },
      data: { ...codeBlockNodeDefaultData, label: "child" },
    };
    useNodeCollapseStore.getState().collapseAll(WF, ["container"]);
    render(<ContainerGraph initialNodes={[container, child]} />);
    expect(graph.nodes.find((node) => node.id === "child")?.hidden).toBe(true);
    const owner = createYamlCommitOwner(WF);
    act(() => {
      registerEditorOwner(owner);
      expect(beginSaveTransaction(owner)).toBe(true);
    });
    await act(async () => {
      await focusBlockTarget("child", {
        getNodes: () => graph.nodes,
        getInternalNode: () => undefined,
        getPaneWidth: () => 1000,
        viewportZoom: 1,
        duration: 0,
        setViewport: vi.fn(),
        selectBlock: vi.fn(),
        expandBlock: (label) =>
          useNodeCollapseStore.getState().expandBlock(WF, label),
        switchBranch: vi.fn(),
        waitForSettle: async () => {},
      });
    });
    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey(WF, "container")
      ],
    ).toBeUndefined();
    expect(graph.nodes.find((node) => node.id === "child")?.hidden).toBe(true);
    act(() => finishSaveTransaction(owner));
    expect(graph.nodes.find((node) => node.id === "child")?.hidden).toBe(false);
    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey(WF, "container")
      ],
    ).toBeUndefined();
  },
);
