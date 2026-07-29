// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type { ConditionalNodeData } from "./types";

const updateNodeData = vi.fn();
const setNodes = vi.fn();
const setEdges = vi.fn();

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useNodes: () => [],
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

import { BranchesEditor } from "./BranchesEditor";

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
  vi.useFakeTimers();
  updateNodeData.mockReset();
  setNodes.mockReset();
  setEdges.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

describe("BranchesEditor", () => {
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
