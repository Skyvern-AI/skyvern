// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";

const mockUseNodesData = vi.fn<(id: string) => unknown>();
const mockUpdateNodeData = vi.fn();
vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useNodesData: (id: string) => mockUseNodesData(id),
    useReactFlow: () => ({
      getNode: () => undefined,
      updateNodeData: mockUpdateNodeData,
      getNodes: () => [],
      getEdges: () => [],
    }),
    useNodes: () => [],
    useEdges: () => [],
  };
});

vi.mock("../../hooks/useIsFirstNodeInWorkflow", () => ({
  useIsFirstBlockInWorkflow: () => false,
}));

import { WaitEditor } from "./WaitEditor";

const buildWaitSlice = (waitInSeconds: string, editable = true) => ({
  id: "wait-1",
  type: "wait" as const,
  data: { label: "Wait 1", editable, waitInSeconds },
});

beforeEach(() => {
  mockUseNodesData.mockReset();
  mockUpdateNodeData.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("WaitEditor reactivity (SKY-9051)", () => {
  test("renders the latest waitInSeconds from useNodesData", () => {
    mockUseNodesData.mockReturnValue(buildWaitSlice("12"));
    const { getByDisplayValue } = render(<WaitEditor blockId="wait-1" />);
    expect(getByDisplayValue("12")).not.toBeNull();
  });

  test("re-renders when the underlying node data updates", () => {
    mockUseNodesData.mockReturnValue(buildWaitSlice("5"));
    const { getByDisplayValue, rerender } = render(
      <WaitEditor blockId="wait-1" />,
    );
    expect(getByDisplayValue("5")).not.toBeNull();

    // Simulate a useUpdate commit landing back through the ReactFlow store:
    // useNodesData starts returning the new data slice, so the controlled
    // Input below must reflect the typed value, not the snapshot.
    mockUseNodesData.mockReturnValue(buildWaitSlice("42"));
    rerender(<WaitEditor blockId="wait-1" />);
    expect(getByDisplayValue("42")).not.toBeNull();
  });

  test("returns null when the node is missing or of the wrong type", () => {
    mockUseNodesData.mockReturnValue(null);
    const { container, rerender } = render(<WaitEditor blockId="wait-1" />);
    expect(container.firstChild).toBeNull();

    mockUseNodesData.mockReturnValue({
      id: "wait-1",
      type: "task" as const,
      data: { label: "x" },
    });
    rerender(<WaitEditor blockId="wait-1" />);
    expect(container.firstChild).toBeNull();
  });

  test("commits typed value through useUpdate -> updateNodeData", () => {
    mockUseNodesData.mockReturnValue(buildWaitSlice("3"));
    const { getByDisplayValue } = render(<WaitEditor blockId="wait-1" />);
    const input = getByDisplayValue("3") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "7" } });
    // The commit shape comes from useUpdate; assert just the patch fields so
    // the test isn't coupled to internal store wiring.
    expect(mockUpdateNodeData).toHaveBeenCalled();
    const call = mockUpdateNodeData.mock.calls[0];
    expect(call?.[0]).toBe("wait-1");
    expect((call?.[1] as { waitInSeconds?: string })?.waitInSeconds).toBe("7");
  });
});
