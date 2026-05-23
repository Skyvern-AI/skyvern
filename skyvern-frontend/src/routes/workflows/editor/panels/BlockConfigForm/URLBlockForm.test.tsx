// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const mockNodes = new Map<
  string,
  | {
      id: string;
      type: string;
      data?: Record<string, unknown>;
    }
  | undefined
>();
const updateNodeData =
  vi.fn<(id: string, data: Record<string, unknown>) => void>();

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useReactFlow: () => ({
      getNode: (id: string) => mockNodes.get(id),
      updateNodeData,
    }),
  };
});

// Stub the textarea to a plain input — the real component pulls in
// parameter-autocomplete, popovers, and react-router state that aren't
// under test here. We only care about the value flow + onChange wiring.
vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: ({
    value,
    onChange,
  }: {
    value: string;
    onChange: (next: string) => void;
    nodeId: string;
    placeholder?: string;
    className?: string;
  }) => (
    <input
      data-testid="url-textarea"
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
}));

import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";
import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { URLBlockForm } from "./URLBlockForm";

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

function setUrlNode(
  id: string,
  data: Partial<{ url: string; editable: boolean; label: string }> = {},
): void {
  mockNodes.set(id, {
    id,
    type: "url",
    data: {
      url: data.url ?? "",
      editable: data.editable ?? true,
      label: data.label ?? "block_1",
      continueOnFailure: false,
      debuggable: true,
      model: null,
    },
  });
}

describe("URLBlockForm (SKY-9389)", () => {
  test("renders the URL field with the node's current url value", () => {
    setUrlNode("u1", { url: "https://example.com" });
    render(<URLBlockForm blockId="u1" />);

    expect(screen.getByText("URL")).toBeDefined();
    const input = screen.getByTestId("url-textarea") as HTMLInputElement;
    expect(input.value).toBe("https://example.com");
  });

  test("returns null for a missing node", () => {
    const { container } = render(<URLBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null for a non-url node type", () => {
    mockNodes.set("u1", {
      id: "u1",
      type: "task",
      data: { url: "x", editable: true, label: "block_1" },
    });
    const { container } = render(<URLBlockForm blockId="u1" />);
    expect(container.firstChild).toBeNull();
  });

  test("propagates edits via updateNodeData on change (byte-identical with inline tile)", () => {
    setUrlNode("u1", { url: "" });
    render(<URLBlockForm blockId="u1" />);

    const input = screen.getByTestId("url-textarea");
    fireEvent.change(input, { target: { value: "https://skyvern.com" } });

    expect(updateNodeData).toHaveBeenCalledWith("u1", {
      url: "https://skyvern.com",
    });
  });

  test("does not propagate edits when the node is non-editable", () => {
    setUrlNode("u1", { url: "", editable: false });
    render(<URLBlockForm blockId="u1" />);

    const input = screen.getByTestId("url-textarea");
    fireEvent.change(input, { target: { value: "https://blocked.com" } });

    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("registers a commit fn on mount and unregisters on unmount", () => {
    setUrlNode("u1");
    const { unmount } = render(<URLBlockForm blockId="u1" />);

    expect(usePendingCommitsStore.getState().commits["u1"]).toBeDefined();

    unmount();
    expect(usePendingCommitsStore.getState().commits["u1"]).toBeUndefined();
  });

  test("debounced save updates the saved-at footer state for this block", () => {
    setUrlNode("u1", { url: "" });
    const { rerender } = render(<URLBlockForm blockId="u1" />);

    expect(
      useSidebarSaveStateStore.getState().getLastUpdatedAt("u1"),
    ).toBeNull();

    setUrlNode("u1", { url: "https://skyvern.com" });
    rerender(<URLBlockForm blockId="u1" />);

    act(() => {
      vi.advanceTimersByTime(500);
    });

    expect(
      useSidebarSaveStateStore.getState().getLastUpdatedAt("u1"),
    ).not.toBeNull();
  });

  test("flush via PendingCommitsStore returns true and persists savedAt", () => {
    setUrlNode("u1", { url: "" });
    const { rerender } = render(<URLBlockForm blockId="u1" />);

    setUrlNode("u1", { url: "https://skyvern.com" });
    rerender(<URLBlockForm blockId="u1" />);

    let flushed = false;
    act(() => {
      flushed = usePendingCommitsStore.getState().flush("u1");
    });
    expect(flushed).toBe(true);
    expect(
      useSidebarSaveStateStore.getState().getLastUpdatedAt("u1"),
    ).not.toBeNull();
  });
});
