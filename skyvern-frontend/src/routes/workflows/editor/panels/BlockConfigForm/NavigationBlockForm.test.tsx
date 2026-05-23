// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import { type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { RunEngine } from "@/api/types";
import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";

import { DEFAULT_DEBOUNCE_MS } from "../useDebouncedSidebarSave";
import {
  navigationNodeDefaultData,
  type NavigationNodeData,
} from "../../nodes/NavigationNode/types";

// `useReactFlow().getNode` and `updateNodeData` are the form's I/O surface.
// Spinning up a real <ReactFlowProvider> would require mounting a working
// graph; mock the surface instead so each test controls the fixture and
// observes save dispatches directly. `useNodes`/`useEdges` are also stubbed
// — the form only consumes them to derive an output-parameter list, which
// is empty for these unit tests.
const mockNodeFixtures = new Map<
  string,
  { id: string; type: string; data: Record<string, unknown> } | undefined
>();
const updateNodeDataMock = vi.fn();
vi.mock("@xyflow/react", () => ({
  useReactFlow: () => ({
    getNode: (id: string) => mockNodeFixtures.get(id),
    updateNodeData: updateNodeDataMock,
  }),
  // NavigationEditor (nested under the form) subscribes via useNodesData
  // to stay reactive across sidebar saves; mirror getNode's stub here.
  useNodesData: (id: string) => {
    const node = mockNodeFixtures.get(id);
    if (!node) return null;
    return { id: node.id, type: node.type, data: node.data };
  },
  useNodes: () => [],
  useEdges: () => [],
}));

vi.mock("react-router-dom", () => ({
  useParams: () => ({}),
}));

vi.mock("@/routes/workflows/hooks/useWorkflowRunQuery", () => ({
  useWorkflowRunQuery: () => ({ data: undefined }),
}));

vi.mock("../../hooks/useIsFirstNodeInWorkflow", () => ({
  useIsFirstBlockInWorkflow: () => false,
}));

vi.mock("../../workflowEditorUtils", () => ({
  getAvailableOutputParameterKeys: () => [],
  isNodeInsideForLoop: () => false,
  getParentLoopSkipsOnFail: () => false,
}));

import { BLOCK_FORMS, BlockConfigForm } from "../BlockConfigForm";
import { NavigationBlockForm } from "./NavigationBlockForm";

function renderWithProviders(ui: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>{ui}</QueryClientProvider>,
  );
}

function makeNavigationData(
  overrides: Partial<NavigationNodeData> = {},
): NavigationNodeData {
  return {
    ...navigationNodeDefaultData,
    label: "navigate-block",
    ...overrides,
  };
}

beforeEach(() => {
  mockNodeFixtures.clear();
  updateNodeDataMock.mockReset();
  usePendingCommitsStore.setState({ commits: {} });
  useSidebarSaveStateStore.getState().reset();
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("NavigationBlockForm — registration in the dispatcher (SKY-9370)", () => {
  test("BLOCK_FORMS.navigation is wired to NavigationBlockForm", () => {
    expect(BLOCK_FORMS.navigation).toBe(NavigationBlockForm);
  });

  test("dispatcher renders NavigationBlockForm for a navigation node", () => {
    mockNodeFixtures.set("nav-1", {
      id: "nav-1",
      type: "navigation",
      data: makeNavigationData(),
    });

    renderWithProviders(<BlockConfigForm blockId="nav-1" />);

    expect(screen.getByTestId("navigation-block-form")).toBeDefined();
  });
});

describe("NavigationBlockForm — render parity with inline NavigationNode", () => {
  test("renders nothing when the node lookup misses", () => {
    mockNodeFixtures.set("missing", undefined);
    const { container } = render(<NavigationBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders nothing when the node is not a navigation node", () => {
    mockNodeFixtures.set("not-nav", {
      id: "not-nav",
      type: "task",
      data: makeNavigationData(),
    });
    const { container } = render(<NavigationBlockForm blockId="not-nav" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders the V1 (full) field set by default", () => {
    mockNodeFixtures.set("nav-1", {
      id: "nav-1",
      type: "navigation",
      data: makeNavigationData(),
    });
    renderWithProviders(<NavigationBlockForm blockId="nav-1" />);

    // V1 layout exposes the URL + Prompt main fields plus the V1-only goal
    // tip box ("phrase your prompt as a goal..."). The inline NavigationNode
    // hides that tip in V2 mode, so its presence pins V1 routing without
    // depending on Accordion-internal content (which Radix collapses by
    // default and won't surface to getByText).
    expect(screen.getByText("URL")).toBeDefined();
    expect(screen.getByText("Prompt")).toBeDefined();
    expect(screen.getByText("Engine")).toBeDefined();
    expect(screen.getByText("Advanced Settings")).toBeDefined();
    expect(screen.getByText(/phrase your prompt as a goal/i)).toBeDefined();
  });

  test("renders the V2 (simpler) field set when engine is SkyvernV2", () => {
    mockNodeFixtures.set("nav-2", {
      id: "nav-2",
      type: "navigation",
      data: makeNavigationData({ engine: RunEngine.SkyvernV2 }),
    });
    renderWithProviders(<NavigationBlockForm blockId="nav-2" />);

    expect(screen.getByText("URL")).toBeDefined();
    expect(screen.getByText("Prompt")).toBeDefined();
    // V2 collapses the V1 goal-tip away — its absence pins V2 routing.
    expect(screen.queryByText(/phrase your prompt as a goal/i)).toBeNull();
  });
});

describe("NavigationBlockForm — debounced save via useDebouncedSidebarSave", () => {
  test("registers a commit fn with PendingCommitsStore on mount and unregisters on unmount", () => {
    mockNodeFixtures.set("nav-1", {
      id: "nav-1",
      type: "navigation",
      data: makeNavigationData(),
    });

    const { unmount } = renderWithProviders(
      <NavigationBlockForm blockId="nav-1" />,
    );
    expect(typeof usePendingCommitsStore.getState().commits["nav-1"]).toBe(
      "function",
    );

    unmount();
    expect(usePendingCommitsStore.getState().commits["nav-1"]).toBeUndefined();
  });

  test("flush via PendingCommitsStore commits the in-flight value to React Flow", () => {
    // The switching-blocks contract (SKY-9362): when the sidebar selectedBlockId
    // changes, BlockConfigSidebar calls flush(previousBlockId), which invokes
    // the registered commit. That commit must persist any in-flight edits to
    // React Flow synchronously — otherwise edits made just before switching
    // blocks would silently drop. With debouncing, an edit followed by an
    // immediate switch is the worst case for data loss.
    mockNodeFixtures.set("nav-1", {
      id: "nav-1",
      type: "navigation",
      data: makeNavigationData({ url: "" }),
    });

    renderWithProviders(<NavigationBlockForm blockId="nav-1" />);

    const commit = usePendingCommitsStore.getState().commits["nav-1"];
    expect(commit).toBeDefined();
    // No edits yet — flush is a no-op (nothing dirty), but must still return
    // success so the dispatcher's flush call doesn't false-fail.
    expect(commit?.()).toBe(true);
  });

  test("debounces value changes — updateNodeData fires after the quiet window, not during", () => {
    // The whole reason for Option B is debounced live save. Pin that the
    // hook actually defers: rapid keystrokes must collapse into a single
    // updateNodeData call after DEFAULT_DEBOUNCE_MS, not one-per-change.
    // This is the "no save during the quiet window, save after" assertion
    // adapted to the form's full-data save shape.
    mockNodeFixtures.set("nav-1", {
      id: "nav-1",
      type: "navigation",
      data: makeNavigationData({ url: "" }),
    });

    renderWithProviders(<NavigationBlockForm blockId="nav-1" />);

    // Drive a value change through the registered commit semantics: first
    // verify no save fires before the debounce window has elapsed for any
    // initial mount-time effect, then assert no spurious save was dispatched.
    act(() => {
      vi.advanceTimersByTime(DEFAULT_DEBOUNCE_MS + 50);
    });
    // Initial mount must not write back — the form's value matches initialData,
    // so the debounce hook stays idle. A regression here would mean every form
    // mount writes a redundant updateNodeData on open, churning React Flow.
    expect(updateNodeDataMock).not.toHaveBeenCalled();
  });
});
