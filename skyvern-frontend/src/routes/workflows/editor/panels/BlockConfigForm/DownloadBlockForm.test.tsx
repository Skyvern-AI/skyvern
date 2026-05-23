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
import { DownloadBlockForm } from "./DownloadBlockForm";

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

function setDownloadNode(
  id: string,
  data: Partial<{ url: string; editable: boolean }> = {},
) {
  mockNodes.set(id, {
    id,
    type: "download",
    data: {
      url: data.url ?? "",
      editable: data.editable ?? true,
      label: "block_1",
      continueOnFailure: false,
      debuggable: true,
      model: null,
    },
  });
}

describe("DownloadBlockForm (SKY-93XX)", () => {
  test("returns null for missing node", () => {
    const { container } = render(<DownloadBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null for wrong node type", () => {
    mockNodes.set("d1", { id: "d1", type: "task", data: { url: "x" } });
    const { container } = render(<DownloadBlockForm blockId="d1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders the file path field with the node's url, disabled", () => {
    setDownloadNode("d1", { url: "/tmp/downloads" });
    render(<DownloadBlockForm blockId="d1" />);
    const input = screen.getByDisplayValue(
      "/tmp/downloads",
    ) as HTMLInputElement;
    expect(input.disabled).toBe(true);
  });

  test("registers a commit fn on mount and unregisters on unmount", () => {
    setDownloadNode("d1");
    const { unmount } = render(<DownloadBlockForm blockId="d1" />);
    expect(usePendingCommitsStore.getState().commits["d1"]).toBeDefined();
    unmount();
    expect(usePendingCommitsStore.getState().commits["d1"]).toBeUndefined();
  });

  test("flush via PendingCommitsStore returns true", () => {
    setDownloadNode("d1");
    render(<DownloadBlockForm blockId="d1" />);
    let ok = false;
    act(() => {
      ok = usePendingCommitsStore.getState().flush("d1");
    });
    expect(ok).toBe(true);
  });
});
