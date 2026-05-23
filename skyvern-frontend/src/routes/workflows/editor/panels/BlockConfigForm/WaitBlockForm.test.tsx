// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// Stub the nodes barrel — the type guard is the only piece this test
// exercises, and pulling the full barrel transitively imports every block
// component plus their providers, none of which are needed to verify
// WaitBlockForm's contract.
vi.mock("../../nodes", () => ({
  isWorkflowBlockNode: (node: { type: string }) =>
    node.type !== "nodeAdder" && node.type !== "start",
}));

const mockGetNode = vi.fn<(id: string) => unknown>();
const mockUpdateNodeData = vi.fn();
vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useReactFlow: () => ({
      getNode: (id: string) => mockGetNode(id),
      updateNodeData: mockUpdateNodeData,
    }),
    // WaitEditor (nested under WaitBlockForm) subscribes to data via
    // useNodesData to stay reactive across sidebar saves. Mirror getNode's
    // stub here so the existing fixture covers both lookups.
    useNodesData: (id: string) => {
      const node = mockGetNode(id) as
        | { id: string; type: string; data: unknown }
        | undefined;
      return node ? { id: node.id, type: node.type, data: node.data } : null;
    },
    useNodes: () => [],
    useEdges: () => [],
  };
});

vi.mock("../../hooks/useIsFirstNodeInWorkflow", () => ({
  useIsFirstBlockInWorkflow: () => false,
}));

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";

import { WaitBlockForm } from "./WaitBlockForm";

const baseWaitNode = (overrides: Record<string, unknown> = {}) => ({
  id: "wait-1",
  type: "wait",
  position: { x: 0, y: 0 },
  data: {
    label: "Wait 1",
    debuggable: true,
    editable: true,
    continueOnFailure: false,
    waitInSeconds: "5",
    model: null,
    ...overrides,
  },
});

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
  mockGetNode.mockReset();
  mockUpdateNodeData.mockReset();
  usePendingCommitsStore.setState({ commits: {} });
  useSidebarSaveStateStore.getState().reset();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("WaitBlockForm (SKY-9375)", () => {
  test("returns null when the node lookup misses", () => {
    mockGetNode.mockReturnValue(undefined);
    const { container } = render(<WaitBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null when the node is not a wait block", () => {
    mockGetNode.mockReturnValue({
      id: "task-1",
      type: "task",
      position: { x: 0, y: 0 },
      data: { label: "Task 1" },
    });
    const { container } = render(<WaitBlockForm blockId="task-1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders the form for a valid wait node", () => {
    mockGetNode.mockReturnValue(baseWaitNode());

    render(<WaitBlockForm blockId="wait-1" />);

    expect(screen.getByTestId("wait-block-form")).toBeDefined();
  });

  test("renders the migrated fields the inline tile exposes", () => {
    // Mirrors the inline WaitNode JSX: a single Wait Time (in seconds)
    // labelled input. If the field disappears here it disappears from the
    // sidebar UI.
    mockGetNode.mockReturnValue(baseWaitNode());
    render(<WaitBlockForm blockId="wait-1" />);

    expect(screen.getByText("Wait Time (in seconds)")).toBeDefined();
    const input = screen.getByRole("textbox") as HTMLInputElement;
    expect(input.value).toBe("5");
  });

  test("dispatches updateNodeData when the wait time field changes", () => {
    mockGetNode.mockReturnValue(baseWaitNode());
    render(<WaitBlockForm blockId="wait-1" />);

    const input = screen.getByRole("textbox") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "30" } });

    expect(mockUpdateNodeData).toHaveBeenCalledWith("wait-1", {
      waitInSeconds: "30",
    });
  });

  test("does not dispatch updates when the node is not editable", () => {
    mockGetNode.mockReturnValue(baseWaitNode({ editable: false }));
    render(<WaitBlockForm blockId="wait-1" />);

    const input = screen.getByRole("textbox") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "999" } });

    expect(mockUpdateNodeData).not.toHaveBeenCalled();
  });

  test("registers a commit on mount and unregisters on unmount", () => {
    mockGetNode.mockReturnValue(baseWaitNode());

    const { unmount } = render(<WaitBlockForm blockId="wait-1" />);
    expect(usePendingCommitsStore.getState().commits["wait-1"]).toBeDefined();

    unmount();
    expect(usePendingCommitsStore.getState().commits["wait-1"]).toBeUndefined();
  });

  test("the registered commit is the debounced-save commit (drives savedAt footer)", () => {
    mockGetNode.mockReturnValue(baseWaitNode());
    render(<WaitBlockForm blockId="wait-1" />);

    // Calling the registered commit must not throw and must report
    // success (no validation errors). The same call is what the sidebar
    // dispatcher invokes on block-switch to flush in-flight edits.
    const commit = usePendingCommitsStore.getState().commits["wait-1"];
    expect(commit).toBeDefined();
    expect(commit?.()).toBe(true);
  });
});
