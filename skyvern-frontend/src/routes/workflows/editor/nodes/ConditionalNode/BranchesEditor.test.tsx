// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import type { Edge, Node } from "@xyflow/react";

import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";

import type { ConditionalNodeData } from "./types";
import { BranchesEditor } from "./BranchesEditor";

const updateNodeData = vi.fn();
const setNodes = vi.fn();
const setEdges = vi.fn();
let nodes: Node[] = [];
let edges: Edge[] = [];

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useNodes: () => nodes,
    useReactFlow: () => ({ setNodes, setEdges, updateNodeData }),
  };
});

vi.mock("@/store/WorkflowHasChangesStore", () => ({
  useWorkflowHasChangesStore: () => ({
    beginInternalUpdate: vi.fn(),
    endInternalUpdate: vi.fn(),
  }),
}));

vi.mock("..", () => ({
  isWorkflowBlockNode: (node: { type: string }) =>
    node.type !== "nodeAdder" && node.type !== "start",
}));

vi.mock("@/components/HelpTooltip", () => ({
  HelpTooltip: () => null,
}));

vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: ({
    value,
    disabled,
  }: {
    value: string;
    disabled?: boolean;
  }) => <textarea value={value} disabled={disabled} readOnly />,
}));

function makeData(
  overrides: Partial<ConditionalNodeData> = {},
): ConditionalNodeData {
  return {
    debuggable: true,
    label: "conditional_1",
    editable: true,
    model: null,
    continueOnFailure: false,
    branches: [
      {
        id: "branch_a",
        criteria: {
          criteria_type: "jinja2_template",
          expression: "{{ total > 100 }}",
          description: null,
        },
        next_block_label: null,
        description: null,
        is_default: false,
      },
      {
        id: "branch_b",
        criteria: {
          criteria_type: "jinja2_template",
          expression: "{{ total > 250 }}",
          description: null,
        },
        next_block_label: null,
        description: null,
        is_default: false,
      },
      {
        id: "branch_default",
        criteria: null,
        next_block_label: null,
        description: null,
        is_default: true,
      },
    ],
    activeBranchId: "branch_a",
    mergeLabel: null,
    ...overrides,
  };
}

beforeEach(() => {
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
  vi.useFakeTimers();
  updateNodeData.mockReset();
  setNodes.mockReset();
  setEdges.mockReset();
  nodes = [];
  edges = [];
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
});

describe("BranchesEditor", () => {
  describe.each(["yaml", "save", "copilot"] as const)("%s lock", (kind) => {
    function renderLockedGraph() {
      const data = makeData();
      nodes = [
        { id: "cond_1", type: "conditional", position: { x: 0, y: 0 }, data },
        ...["start", "nodeAdder", "wait"].map((type) => ({
          id: type,
          type,
          parentId: "cond_1",
          position: { x: 0, y: 0 },
          data: {
            conditionalNodeId: "cond_1",
            conditionalBranchId: "branch_a",
          },
        })),
      ];
      edges = [
        {
          id: "branch_a-edge",
          source: "start",
          target: "wait",
          data: {
            conditionalNodeId: "cond_1",
            conditionalBranchId: "branch_a",
          },
        },
      ];
      setNodes.mockImplementation((update: (current: Node[]) => Node[]) => {
        nodes = update(nodes);
      });
      setEdges.mockImplementation((update: (current: Edge[]) => Edge[]) => {
        edges = update(edges);
      });
      updateNodeData.mockImplementation(
        (_id: string, patch: Partial<ConditionalNodeData>) => {
          Object.assign(data, patch);
        },
      );
      render(<BranchesEditor nodeId="cond_1" data={data} />);
      act(() => {
        useWorkflowYamlEditorStore.setState({
          commitInProgress: kind !== "copilot",
          copilotAcceptance: kind === "copilot" ? Symbol("turn") : null,
          lockKind: kind,
        });
      });
      setNodes.mockClear();
      setEdges.mockClear();
      updateNodeData.mockClear();
      return {
        data,
        before: structuredClone({ nodes, edges, branches: data.branches }),
      };
    }

    test("Remove preserves nodes, edges, and branches while locked", () => {
      const { data, before } = renderLockedGraph();
      fireEvent.keyDown(screen.getAllByTitle("Branch options")[0]!, {
        key: "Enter",
      });
      const removeItem = screen.getByRole("menuitem", { name: "Remove" });
      expect(removeItem.getAttribute("aria-disabled")).toBe("true");
      fireEvent.click(removeItem);

      expect({ nodes, edges, branches: data.branches }).toEqual(before);
      expect(setNodes).not.toHaveBeenCalled();
      expect(setEdges).not.toHaveBeenCalled();
      expect(updateNodeData).not.toHaveBeenCalled();
    });

    test("Add inserts no edge or branch while locked", () => {
      const { data, before } = renderLockedGraph();
      fireEvent.click(screen.getByTitle("Add new condition"));

      expect({ nodes, edges, branches: data.branches }).toEqual(before);
      expect(setEdges).not.toHaveBeenCalled();
      expect(updateNodeData).not.toHaveBeenCalled();
      expect(
        (screen.getByTitle("Add new condition") as HTMLButtonElement).disabled,
      ).toBe(true);
    });
  });

  test("switches the active branch in read-only mode", () => {
    render(
      <BranchesEditor nodeId="cond_1" data={makeData({ editable: false })} />,
    );

    const inactiveTab = screen.getByText("B • Else If").closest("button")!;
    expect(inactiveTab.disabled).toBe(false);

    updateNodeData.mockClear();
    fireEvent.click(inactiveTab);

    expect(updateNodeData).toHaveBeenCalledWith("cond_1", {
      activeBranchId: "branch_b",
    });
  });

  test("switches the active branch in editable mode", () => {
    render(
      <BranchesEditor nodeId="cond_1" data={makeData({ editable: true })} />,
    );

    updateNodeData.mockClear();
    fireEvent.click(screen.getByText("B • Else If").closest("button")!);

    expect(updateNodeData).toHaveBeenCalledWith("cond_1", {
      activeBranchId: "branch_b",
    });
  });

  test("hides edit affordances in read-only mode", () => {
    render(
      <BranchesEditor nodeId="cond_1" data={makeData({ editable: false })} />,
    );

    expect(
      (screen.getByTitle("Add new condition") as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(screen.queryByTitle("Branch options")).toBeNull();
  });

  test("shows edit affordances in editable mode", () => {
    render(
      <BranchesEditor nodeId="cond_1" data={makeData({ editable: true })} />,
    );

    expect(
      (screen.getByTitle("Add new condition") as HTMLButtonElement).disabled,
    ).toBe(false);
    expect(screen.getAllByTitle("Branch options").length).toBeGreaterThan(0);
  });
});

test.each(["yaml", "copilot"] as const)(
  "initializes conditional branches after the %s lock releases",
  (kind) => {
    useWorkflowYamlEditorStore.setState({
      commitInProgress: kind === "yaml",
      copilotAcceptance: kind === "copilot" ? Symbol("turn") : null,
    });
    const data = makeData({ activeBranchId: null });
    data.branches = data.branches.filter((branch) => !branch.is_default);
    render(<BranchesEditor nodeId="cond_1" data={data} />);
    expect(updateNodeData).not.toHaveBeenCalled();
    act(() =>
      useWorkflowYamlEditorStore.setState({
        commitInProgress: false,
        copilotAcceptance: null,
      }),
    );
    expect(updateNodeData).toHaveBeenCalledWith("cond_1", {
      branches: [
        ...data.branches,
        expect.objectContaining({ is_default: true }),
      ],
    });
    expect(updateNodeData).toHaveBeenCalledWith("cond_1", {
      activeBranchId: "branch_a",
    });
  },
);
