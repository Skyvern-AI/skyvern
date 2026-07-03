// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type { ConditionalNode } from "../../nodes/ConditionalNode/types";

const mockNodes = new Map<
  string,
  { id: string; type: string; data?: Record<string, unknown> } | undefined
>();
const updateNodeData = vi.fn();
const isWorkflowBlockNodeMock = vi.fn<(node: { type: string }) => boolean>(
  (node) => node.type !== "nodeAdder" && node.type !== "start",
);

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useNodesData: (id: string) => {
      const node = mockNodes.get(id);
      return node ? { id: node.id, type: node.type, data: node.data } : null;
    },
    useReactFlow: () => ({
      updateNodeData,
    }),
  };
});

vi.mock("../../nodes", () => ({
  isWorkflowBlockNode: (node: { type: string }) =>
    isWorkflowBlockNodeMock(node),
}));

vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: ({
    value,
    disabled,
    onChange,
    placeholder,
  }: {
    value: string;
    disabled?: boolean;
    onChange: (v: string) => void;
    placeholder?: string;
    nodeId: string;
    className?: string;
  }) => (
    <textarea
      aria-label={placeholder}
      disabled={disabled}
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
}));

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";
import { ConditionalBlockForm } from "./ConditionalBlockForm";

beforeEach(() => {
  vi.useFakeTimers();
  mockNodes.clear();
  updateNodeData.mockReset();
  isWorkflowBlockNodeMock.mockReset();
  isWorkflowBlockNodeMock.mockImplementation(
    (node) => node.type !== "nodeAdder" && node.type !== "start",
  );
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
            expression: "{{ total > 100 }}",
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

describe("ConditionalBlockForm", () => {
  test("returns null for missing node", () => {
    const { container } = render(<ConditionalBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null for wrong node type", () => {
    mockNodes.set("c1", { id: "c1", type: "task", data: {} });
    const { container } = render(<ConditionalBlockForm blockId="c1" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null for non-workflow conditional nodes", () => {
    isWorkflowBlockNodeMock.mockReturnValue(false);
    setConditionalNode("c1");

    const { container } = render(<ConditionalBlockForm blockId="c1" />);

    expect(container.firstChild).toBeNull();
    expect(isWorkflowBlockNodeMock).toHaveBeenCalledWith(
      expect.objectContaining({ type: "conditional" }),
    );
  });

  test("renders branch prompts and the default branch in the sidebar", () => {
    setConditionalNode("c1");
    render(<ConditionalBlockForm blockId="c1" />);

    expect(screen.getByText("A • If")).toBeDefined();
    expect(screen.getByText("B • Else")).toBeDefined();
    expect(screen.getByText("Active")).toBeDefined();
    expect(screen.getByDisplayValue("{{ total > 100 }}")).toBeDefined();
    expect(
      (
        screen.getByDisplayValue(
          "Executed when no other condition matches",
        ) as HTMLTextAreaElement
      ).disabled,
    ).toBe(true);
  });

  test("preserves every default branch when ordering branch prompts", () => {
    setConditionalNode("c1", {
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
          id: "branch_default_1",
          criteria: null,
          next_block_label: null,
          description: null,
          is_default: true,
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
          id: "branch_default_2",
          criteria: null,
          next_block_label: null,
          description: null,
          is_default: true,
        },
      ],
    });

    render(<ConditionalBlockForm blockId="c1" />);

    expect(screen.getByText("A • If")).toBeDefined();
    expect(screen.getByText("B • Else If")).toBeDefined();
    expect(screen.getByText("C • Else")).toBeDefined();
    expect(screen.getByText("D • Else")).toBeDefined();
    expect(
      screen.getAllByDisplayValue("Executed when no other condition matches"),
    ).toHaveLength(2);
  });

  test("editing a branch prompt propagates via updateNodeData", () => {
    setConditionalNode("c1");
    render(<ConditionalBlockForm blockId="c1" />);

    fireEvent.change(screen.getByDisplayValue("{{ total > 100 }}"), {
      target: { value: "{{ total > 250 }}" },
    });

    expect(updateNodeData).toHaveBeenCalledWith("c1", {
      branches: [
        expect.objectContaining({
          id: "branch_a",
          criteria: expect.objectContaining({
            expression: "{{ total > 250 }}",
          }),
        }),
        expect.objectContaining({
          id: "branch_default",
          criteria: null,
        }),
      ],
    });
  });

  test("non-editable branch prompts do not propagate edits", () => {
    setConditionalNode("c1", { editable: false });
    render(<ConditionalBlockForm blockId="c1" />);

    fireEvent.change(screen.getByDisplayValue("{{ total > 100 }}"), {
      target: { value: "{{ total > 250 }}" },
    });

    expect(updateNodeData).not.toHaveBeenCalled();
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
});
