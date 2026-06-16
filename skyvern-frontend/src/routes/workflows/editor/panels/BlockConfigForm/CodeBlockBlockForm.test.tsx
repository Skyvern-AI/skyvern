// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

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
    useNodes: () => [],
    useEdges: () => [],
  };
});

vi.mock("@/routes/workflows/components/CodeEditor", () => ({
  CodeEditor: ({
    value,
    onChange,
    language,
  }: {
    value: string;
    onChange: (v: string) => void;
    language: string;
  }) => (
    <textarea
      data-testid="code-editor"
      data-language={language}
      value={value}
      onChange={(e) => onChange(e.target.value)}
    />
  ),
}));

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";

import { CodeBlockBlockForm } from "./CodeBlockBlockForm";

const baseCodeBlockNode = (overrides: Record<string, unknown> = {}) => ({
  id: "code-1",
  type: "codeBlock",
  position: { x: 0, y: 0 },
  data: {
    label: "Code 1",
    debuggable: true,
    editable: true,
    code: "x = 5",
    parameterKeys: [],
    continueOnFailure: false,
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

describe("CodeBlockBlockForm (SKY-9380)", () => {
  test("returns null when the node lookup misses", () => {
    mockGetNode.mockReturnValue(undefined);
    const { container } = render(<CodeBlockBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null when the node is not a code block", () => {
    mockGetNode.mockReturnValue({
      id: "task-1",
      type: "task",
      position: { x: 0, y: 0 },
      data: { label: "Task 1" },
    });
    const { container } = render(<CodeBlockBlockForm blockId="task-1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders the form for a valid code block node", () => {
    mockGetNode.mockReturnValue(baseCodeBlockNode());
    render(<CodeBlockBlockForm blockId="code-1" />);
    expect(screen.getByTestId("code-block-block-form")).toBeDefined();
  });

  test("renders the migrated fields the inline tile exposes", () => {
    // Mirrors the inline CodeBlockNode JSX: the Inputs selector and a python
    // CodeEditor. If a field disappears here it disappears from the sidebar UI.
    mockGetNode.mockReturnValue(baseCodeBlockNode());
    render(<CodeBlockBlockForm blockId="code-1" />);

    expect(screen.getByText("Code Input")).toBeDefined();
    expect(screen.getByText("Inputs")).toBeDefined();
    expect(screen.getByTestId("code-editor")).toBeDefined();
    expect(
      screen.getByTestId("code-editor").getAttribute("data-language"),
    ).toBe("python");
  });

  test("dispatches updateNodeData when the code field changes", () => {
    mockGetNode.mockReturnValue(baseCodeBlockNode());
    render(<CodeBlockBlockForm blockId="code-1" />);

    const codeEditor = screen.getByTestId("code-editor") as HTMLTextAreaElement;
    fireEvent.change(codeEditor, { target: { value: "y = 10" } });

    expect(mockUpdateNodeData).toHaveBeenCalledWith("code-1", {
      code: "y = 10",
    });
  });

  test("does not dispatch updates when the node is not editable", () => {
    mockGetNode.mockReturnValue(baseCodeBlockNode({ editable: false }));
    render(<CodeBlockBlockForm blockId="code-1" />);

    const codeEditor = screen.getByTestId("code-editor") as HTMLTextAreaElement;
    fireEvent.change(codeEditor, { target: { value: "blocked" } });

    expect(mockUpdateNodeData).not.toHaveBeenCalled();
  });

  test("registers a commit on mount and unregisters on unmount", () => {
    mockGetNode.mockReturnValue(baseCodeBlockNode());

    const { unmount } = render(<CodeBlockBlockForm blockId="code-1" />);
    expect(usePendingCommitsStore.getState().commits["code-1"]).toBeDefined();

    unmount();
    expect(usePendingCommitsStore.getState().commits["code-1"]).toBeUndefined();
  });

  test("the registered commit is the debounced-save commit (drives savedAt footer)", () => {
    mockGetNode.mockReturnValue(baseCodeBlockNode());
    render(<CodeBlockBlockForm blockId="code-1" />);

    const commit = usePendingCommitsStore.getState().commits["code-1"];
    expect(commit).toBeDefined();
    expect(commit?.()).toBe(true);
  });

  test("stamps the savedAt footer when code-first fields change", () => {
    const codeFirstFields = {
      prompt: "Open {{ url }}",
      steps: [{ description: "Open the page", action_type: "goto_url" }],
    };
    mockGetNode.mockReturnValue(baseCodeBlockNode(codeFirstFields));
    const { rerender } = render(<CodeBlockBlockForm blockId="code-1" />);

    mockGetNode.mockReturnValue(
      baseCodeBlockNode({ ...codeFirstFields, prompt: "Open {{ link }}" }),
    );
    rerender(<CodeBlockBlockForm blockId="code-1" />);
    vi.advanceTimersByTime(400);

    expect(
      useSidebarSaveStateStore.getState().getLastUpdatedAt("code-1"),
    ).not.toBeNull();
  });
});
