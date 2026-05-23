// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { type ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

// The form transitively imports the nodes barrel (via `AppNode` and
// `isWorkflowBlockNode`) plus several React Flow utility hooks. Stub the
// surface the form actually consumes so the test does not need a real
// `<ReactFlowProvider>` wired up.
const isWorkflowBlockNodeMock = vi.fn<(node: { type: string }) => boolean>(
  () => true,
);
vi.mock("../../nodes", () => ({
  isWorkflowBlockNode: (node: { type: string }) =>
    isWorkflowBlockNodeMock(node),
}));

const mockNodeFixtures = new Map<
  string,
  { id: string; type: string; data?: Record<string, unknown> } | undefined
>();
const updateNodeDataMock = vi.fn();

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useReactFlow: () => ({
      getNode: (id: string) => mockNodeFixtures.get(id),
      updateNodeData: updateNodeDataMock,
    }),
    useNodes: () =>
      Array.from(mockNodeFixtures.values()).filter(
        (
          n,
        ): n is { id: string; type: string; data?: Record<string, unknown> } =>
          n !== undefined,
      ),
    useEdges: () => [],
  };
});

vi.mock("react-router-dom", () => ({
  useParams: () => ({}),
}));

vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: null }),
}));

vi.mock("../../hooks/useIsFirstNodeInWorkflow", () => ({
  useIsFirstBlockInWorkflow: () => false,
}));

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";

import { taskNodeDefaultData } from "../../nodes/TaskNode/types";
import { TaskBlockForm } from "./TaskBlockForm";

const blockId = "task-1";

function fixtureNode(overrides: Record<string, unknown> = {}) {
  return {
    id: blockId,
    type: "task",
    data: { ...taskNodeDefaultData, label: "task_block", ...overrides },
  };
}

// The form's `WorkflowBlockInputTextarea` transitively renders
// `ImprovePrompt`, which calls `useMutation` and requires a
// QueryClientProvider in scope. Wrap each render with a fresh client so
// the mutation hook can mount without polluting other tests.
function renderWithProviders(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>{ui}</QueryClientProvider>,
  );
}

beforeEach(() => {
  isWorkflowBlockNodeMock.mockReset();
  isWorkflowBlockNodeMock.mockImplementation(() => true);
  mockNodeFixtures.clear();
  updateNodeDataMock.mockReset();
  usePendingCommitsStore.setState({ commits: {} });
});

afterEach(() => {
  cleanup();
});

describe("TaskBlockForm (SKY-9368)", () => {
  test("returns null when the dispatched node is missing", () => {
    mockNodeFixtures.set(blockId, undefined);
    const { container } = renderWithProviders(
      <TaskBlockForm blockId={blockId} />,
    );
    expect(container.firstChild).toBeNull();
  });

  test("returns null when the node is not a workflow block (defensive)", () => {
    isWorkflowBlockNodeMock.mockReturnValue(false);
    mockNodeFixtures.set(blockId, { id: blockId, type: "start" });
    const { container } = renderWithProviders(
      <TaskBlockForm blockId={blockId} />,
    );
    expect(container.firstChild).toBeNull();
  });

  test("renders the form scaffold for a task node", () => {
    mockNodeFixtures.set(blockId, fixtureNode());
    renderWithProviders(<TaskBlockForm blockId={blockId} />);

    const root = screen.getByTestId("task-block-form");
    expect(root.getAttribute("data-block-id")).toBe(blockId);
    // Pin the three accordion section headers from the inline TaskNode form
    // so a future visual refactor that drops one is caught.
    expect(screen.getByText("Content")).toBeDefined();
    expect(screen.getByText("Extraction")).toBeDefined();
    expect(screen.getByText("Advanced Settings")).toBeDefined();
  });

  test("registers a no-op commit on mount and unregisters on unmount", () => {
    // Field-level immediate persistence means `commit()` has no pending
    // edits to flush — but the dispatcher (SKY-9361) still relies on
    // every migrated form *registering* with PendingCommitsStore so that
    // a future Option-B migration can drop in without surprising the
    // switching-blocks orchestration. Pin both the register on mount and
    // the unregister on unmount.
    mockNodeFixtures.set(blockId, fixtureNode());
    const { unmount } = renderWithProviders(
      <TaskBlockForm blockId={blockId} />,
    );

    const commit = usePendingCommitsStore.getState().commits[blockId];
    expect(commit).toBeDefined();
    expect(commit?.()).toBe(true);

    unmount();
    expect(usePendingCommitsStore.getState().commits[blockId]).toBeUndefined();
  });

  test("flushing a registered task block via the store returns true (no pending edits)", () => {
    // The dispatcher calls `flush(previousBlockId)` on block switch.
    // For this Option-A form, flushing must be a non-throwing success
    // signal so the orchestration can immediately remount the next form.
    mockNodeFixtures.set(blockId, fixtureNode());
    renderWithProviders(<TaskBlockForm blockId={blockId} />);

    expect(usePendingCommitsStore.getState().flush(blockId)).toBe(true);
  });
});
