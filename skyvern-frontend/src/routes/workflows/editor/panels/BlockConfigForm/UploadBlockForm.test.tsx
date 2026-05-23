// @vitest-environment jsdom

import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

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
  };
});

import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";
import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { UploadBlockForm } from "./UploadBlockForm";

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

function setUploadNode(
  id: string,
  data: Partial<{ path: string; editable: boolean }> = {},
) {
  mockNodes.set(id, {
    id,
    type: "upload",
    data: {
      path: data.path ?? "",
      editable: data.editable ?? true,
      label: "block_1",
      continueOnFailure: false,
      debuggable: true,
      model: null,
    },
  });
}

describe("UploadBlockForm (SKY-93XX)", () => {
  test("returns null for missing node", () => {
    const { container } = render(<UploadBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null for wrong node type", () => {
    mockNodes.set("u1", { id: "u1", type: "task", data: { path: "x" } });
    const { container } = render(<UploadBlockForm blockId="u1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders the file path field with the node's path, disabled", () => {
    setUploadNode("u1", { path: "/tmp/uploads" });
    render(<UploadBlockForm blockId="u1" />);
    const input = screen.getByDisplayValue("/tmp/uploads") as HTMLInputElement;
    expect(input.disabled).toBe(true);
  });

  test("registers a commit fn on mount and unregisters on unmount", () => {
    setUploadNode("u1");
    const { unmount } = render(<UploadBlockForm blockId="u1" />);
    expect(usePendingCommitsStore.getState().commits["u1"]).toBeDefined();
    unmount();
    expect(usePendingCommitsStore.getState().commits["u1"]).toBeUndefined();
  });

  test("flush via PendingCommitsStore returns true", () => {
    setUploadNode("u1");
    render(<UploadBlockForm blockId="u1" />);
    let ok = false;
    act(() => {
      ok = usePendingCommitsStore.getState().flush("u1");
    });
    expect(ok).toBe(true);
  });
});
