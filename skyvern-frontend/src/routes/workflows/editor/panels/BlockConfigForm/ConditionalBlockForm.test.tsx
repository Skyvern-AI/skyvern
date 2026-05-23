// @vitest-environment jsdom

import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type { ConditionalNode } from "../../nodes/ConditionalNode/types";

const mockNodes = new Map<
  string,
  { id: string; type: string; data?: Record<string, unknown> } | undefined
>();
const updateNodeData = vi.fn();

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useReactFlow: () => ({
      getNode: (id: string) => mockNodes.get(id),
      updateNodeData,
    }),
    useNodes: () => Array.from(mockNodes.values()),
    useEdges: () => [],
  };
});

vi.mock("../../nodes/ConditionalNode/BranchesEditor", () => ({
  BranchesEditor: (props: {
    nodeId: string;
    data: { branches: Array<unknown>; activeBranchId: string | null };
  }) => (
    <div
      data-testid="branches-editor"
      data-node-id={props.nodeId}
      data-branches-count={String(props.data.branches.length)}
      data-active-branch={props.data.activeBranchId ?? ""}
    />
  ),
}));

import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";
import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { ConditionalBlockForm } from "./ConditionalBlockForm";

beforeEach(() => {
  vi.useFakeTimers();
  mockNodes.clear();
  updateNodeData.mockReset();
  usePendingCommitsStore.setState({ commits: {} });
  useSidebarSaveStateStore.getState().reset();
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

function setConditionalNode(
  id: string,
  overrides: Partial<ConditionalNode["data"]> = {},
) {
  mockNodes.set(id, {
    id,
    type: "conditional",
    data: {
      debuggable: true,
      label: "conditional_1",
      editable: true,
      model: null,
      continueOnFailure: false,
      nextLoopOnFailure: false,
      branches: [
        {
          id: "branch_a",
          criteria: {
            criteria_type: "jinja2_template",
            expression: "",
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
    },
  });
}

describe("ConditionalBlockForm (SKY-9361)", () => {
  test("returns null for missing node", () => {
    const { container } = render(<ConditionalBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null for wrong node type", () => {
    mockNodes.set("c1", { id: "c1", type: "task", data: {} });
    const { container } = render(<ConditionalBlockForm blockId="c1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders BranchesEditor with the blockId and node data", () => {
    setConditionalNode("c1");
    render(<ConditionalBlockForm blockId="c1" />);

    const editor = screen.getByTestId("branches-editor");
    expect(editor.getAttribute("data-node-id")).toBe("c1");
    expect(editor.getAttribute("data-branches-count")).toBe("2");
    expect(editor.getAttribute("data-active-branch")).toBe("branch_a");
  });

  test("registers/unregisters commit on mount/unmount", () => {
    setConditionalNode("c1");
    const { unmount } = render(<ConditionalBlockForm blockId="c1" />);
    expect(usePendingCommitsStore.getState().commits["c1"]).toBeDefined();
    unmount();
    expect(usePendingCommitsStore.getState().commits["c1"]).toBeUndefined();
  });

  test("flush via PendingCommitsStore returns true", () => {
    setConditionalNode("c1");
    render(<ConditionalBlockForm blockId="c1" />);

    let ok = false;
    act(() => {
      ok = usePendingCommitsStore.getState().flush("c1");
    });
    expect(ok).toBe(true);
  });

  test("propagates updated activeBranchId to BranchesEditor on rerender", () => {
    setConditionalNode("c1", { activeBranchId: "branch_a" });
    const { rerender } = render(<ConditionalBlockForm blockId="c1" />);
    expect(
      screen.getByTestId("branches-editor").getAttribute("data-active-branch"),
    ).toBe("branch_a");

    setConditionalNode("c1", { activeBranchId: "branch_default" });
    rerender(<ConditionalBlockForm blockId="c1" />);

    expect(
      screen.getByTestId("branches-editor").getAttribute("data-active-branch"),
    ).toBe("branch_default");
  });
});
