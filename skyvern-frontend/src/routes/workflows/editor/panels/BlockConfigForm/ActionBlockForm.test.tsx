// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// Stub the nodes barrel — the type guard is the only piece this test
// exercises, and pulling the full barrel transitively imports every block
// component plus their providers, none of which are needed to verify
// ActionBlockForm's contract.
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
    // ActionEditor (nested under the form) subscribes to data via useNodesData
    // to stay reactive across sidebar saves; mirror getNode's stub here.
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

vi.mock("../../workflowEditorUtils", () => ({
  getAvailableOutputParameterKeys: () => [],
  isNodeInsideForLoop: () => false,
  getParentLoopSkipsOnFail: () => false,
}));

// Stub heavy child components so the test stays focused on the form
// contract (registration, dispatch, field presence) rather than the
// internals of the shared input/selector library.
vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: ({
    value,
    onChange,
    placeholder,
    nodeId,
  }: {
    value: string;
    onChange: (v: string) => void;
    placeholder?: string;
    nodeId: string;
  }) => (
    <textarea
      data-testid={`textarea-${placeholder ?? "field"}`}
      data-node-id={nodeId}
      value={value}
      onChange={(e) => onChange(e.target.value)}
    />
  ),
}));
vi.mock("@/components/WorkflowBlockInput", () => ({
  WorkflowBlockInput: ({
    value,
    onChange,
    placeholder,
  }: {
    value: string;
    onChange: (v: string) => void;
    placeholder?: string;
  }) => (
    <input
      data-testid={`input-${placeholder ?? "field"}`}
      value={value}
      onChange={(e) => onChange(e.target.value)}
    />
  ),
}));
vi.mock("@/components/ModelSelector", () => ({
  ModelSelector: () => <div data-testid="model-selector" />,
}));
vi.mock("@/components/EngineSelector", () => ({
  RunEngineSelector: () => <div data-testid="engine-selector" />,
}));
vi.mock("@/routes/workflows/editor/ErrorCodeMappingEditor", () => ({
  ErrorCodeMappingEditor: () => <div data-testid="error-code-mapping-editor" />,
}));
vi.mock("../../nodes/TaskNode/ParametersMultiSelect", () => ({
  ParametersMultiSelect: () => <div data-testid="parameters-multi-select" />,
}));
vi.mock("../../nodes/components/BlockExecutionOptions", () => ({
  BlockExecutionOptions: () => <div data-testid="block-execution-options" />,
}));
vi.mock("../../nodes/DisableCache", () => ({
  DisableCache: () => <div data-testid="disable-cache" />,
}));

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";

import { ActionBlockForm } from "./ActionBlockForm";

const baseActionNode = (overrides: Record<string, unknown> = {}) => ({
  id: "action-1",
  type: "action",
  position: { x: 0, y: 0 },
  data: {
    label: "Action 1",
    debuggable: true,
    editable: true,
    url: "https://example.com",
    navigationGoal: "click the button",
    errorCodeMapping: "null",
    maxRetries: null,
    allowDownloads: false,
    downloadSuffix: null,
    parameterKeys: [],
    totpVerificationUrl: null,
    totpIdentifier: null,
    continueOnFailure: false,
    nextLoopOnFailure: false,
    disableCache: false,
    engine: null,
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

describe("ActionBlockForm (SKY-9373)", () => {
  test("returns null when the node lookup misses", () => {
    mockGetNode.mockReturnValue(undefined);
    const { container } = render(<ActionBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null when the node is not an action block", () => {
    mockGetNode.mockReturnValue({
      id: "task-1",
      type: "task",
      position: { x: 0, y: 0 },
      data: { label: "Task 1" },
    });
    const { container } = render(<ActionBlockForm blockId="task-1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders the form for a valid action node", () => {
    mockGetNode.mockReturnValue(baseActionNode());

    render(<ActionBlockForm blockId="action-1" />);

    expect(screen.getByTestId("action-block-form")).toBeDefined();
  });

  test("renders the migrated fields the inline tile exposes", () => {
    // Mirrors the inline ActionNode JSX: URL, action instruction (textarea
    // with the navigation-goal placeholder), informational tip box,
    // model/engine selectors, parameters multi-select, error-code mapping
    // toggle, execution options, disable-cache, file-name input, and 2FA
    // identifier + verification-url textareas. If a field disappears here
    // it disappears from the sidebar UI. The Advanced Settings accordion
    // is collapsed by default — assert the trigger plus the
    // always-visible fields, then open the accordion and assert the rest.
    mockGetNode.mockReturnValue(baseActionNode());
    render(<ActionBlockForm blockId="action-1" />);

    expect(screen.getByText("URL")).toBeDefined();
    expect(screen.getByText("Action Instruction")).toBeDefined();
    expect(screen.getByText("Advanced Settings")).toBeDefined();

    fireEvent.click(screen.getByText("Advanced Settings"));

    expect(screen.getByText("Engine")).toBeDefined();
    expect(screen.getByText("Error Messages")).toBeDefined();
    expect(screen.getByText("Complete on Download")).toBeDefined();
    expect(screen.getByText("File Name")).toBeDefined();
    expect(screen.getByText("2FA Identifier")).toBeDefined();
    expect(screen.getByText("2FA Verification URL")).toBeDefined();
    expect(screen.getByTestId("model-selector")).toBeDefined();
    expect(screen.getByTestId("engine-selector")).toBeDefined();
    expect(screen.getByTestId("parameters-multi-select")).toBeDefined();
    expect(screen.getByTestId("block-execution-options")).toBeDefined();
    expect(screen.getByTestId("disable-cache")).toBeDefined();
  });

  test("dispatches updateNodeData when the URL field changes", () => {
    mockGetNode.mockReturnValue(baseActionNode());
    render(<ActionBlockForm blockId="action-1" />);

    const urlField = screen
      .getAllByRole("textbox")
      .find(
        (el) => (el as HTMLTextAreaElement).value === "https://example.com",
      ) as HTMLTextAreaElement;
    fireEvent.change(urlField, { target: { value: "https://changed.com" } });

    expect(mockUpdateNodeData).toHaveBeenCalledWith("action-1", {
      url: "https://changed.com",
    });
  });

  test("does not dispatch updates when the node is not editable", () => {
    mockGetNode.mockReturnValue(baseActionNode({ editable: false }));
    render(<ActionBlockForm blockId="action-1" />);

    const urlField = screen
      .getAllByRole("textbox")
      .find(
        (el) => (el as HTMLTextAreaElement).value === "https://example.com",
      ) as HTMLTextAreaElement;
    fireEvent.change(urlField, { target: { value: "blocked" } });

    expect(mockUpdateNodeData).not.toHaveBeenCalled();
  });

  test("registers a commit on mount and unregisters on unmount", () => {
    mockGetNode.mockReturnValue(baseActionNode());

    const { unmount } = render(<ActionBlockForm blockId="action-1" />);
    expect(usePendingCommitsStore.getState().commits["action-1"]).toBeDefined();

    unmount();
    expect(
      usePendingCommitsStore.getState().commits["action-1"],
    ).toBeUndefined();
  });

  test("the registered commit is the debounced-save commit (drives savedAt footer)", () => {
    mockGetNode.mockReturnValue(baseActionNode());
    render(<ActionBlockForm blockId="action-1" />);

    // Calling the registered commit must not throw and must report
    // success (no validation errors). The same call is what the sidebar
    // dispatcher invokes on block-switch to flush in-flight edits.
    const commit = usePendingCommitsStore.getState().commits["action-1"];
    expect(commit).toBeDefined();
    expect(commit?.()).toBe(true);
  });
});
