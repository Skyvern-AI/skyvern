// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const isWorkflowBlockNodeMock = vi.fn<(node: { type: string }) => boolean>(
  () => true,
);
const updateNodeDataMock = vi.fn<(id: string, updates: object) => void>();
const mockNodeFixtures = new Map<
  string,
  { id: string; type: string; data: Record<string, unknown> } | undefined
>();

vi.mock("../../nodes", () => ({
  isWorkflowBlockNode: (node: { type: string }) =>
    isWorkflowBlockNodeMock(node),
}));

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useReactFlow: () => ({
      getNode: (id: string) => mockNodeFixtures.get(id),
      updateNodeData: (id: string, updates: object) => {
        updateNodeDataMock(id, updates);
        const existing = mockNodeFixtures.get(id);
        if (existing) {
          mockNodeFixtures.set(id, {
            ...existing,
            data: { ...existing.data, ...updates },
          });
        }
      },
    }),
    useNodesData: (id: string) => {
      const node = mockNodeFixtures.get(id);
      return node ? { id: node.id, type: node.type, data: node.data } : null;
    },
  };
});

// Field components depend on browser-only globals (Popover positioning,
// AutoResizingTextarea measurement). Stub them to plain HTML so the form
// can mount in jsdom without setting up a full editor harness.
vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: ({
    value,
    onChange,
    placeholder,
  }: {
    value: string;
    onChange: (value: string) => void;
    placeholder?: string;
  }) => (
    <textarea
      data-testid={`textarea-${placeholder ?? "field"}`}
      value={value}
      placeholder={placeholder}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
}));

vi.mock("@/components/ModelSelector", () => ({
  ModelSelector: ({
    value,
    onChange,
  }: {
    value: unknown;
    onChange: (value: unknown) => void;
  }) => (
    <button
      type="button"
      data-testid="model-selector"
      data-value={JSON.stringify(value)}
      onClick={() => onChange({ provider: "openai", model_name: "gpt-test" })}
    >
      model
    </button>
  ),
}));

vi.mock("@/components/HelpTooltip", () => ({
  HelpTooltip: () => null,
}));

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";
import { DEFAULT_DEBOUNCE_MS } from "../useDebouncedSidebarSave";
import { Taskv2BlockForm } from "./Taskv2BlockForm";

const FULL_DATA = {
  debuggable: true,
  label: "my-task-v2",
  continueOnFailure: false,
  editable: true,
  prompt: "navigate and extract",
  url: "https://example.com",
  model: null,
  totpIdentifier: null,
  totpVerificationUrl: null,
  maxSteps: 25,
  disableCache: false,
  maxScreenshotScrolls: null,
};

beforeEach(() => {
  vi.useFakeTimers();
  isWorkflowBlockNodeMock.mockReset();
  isWorkflowBlockNodeMock.mockImplementation(() => true);
  updateNodeDataMock.mockReset();
  mockNodeFixtures.clear();
  usePendingCommitsStore.setState({ commits: {} });
  useSidebarSaveStateStore.getState().reset();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function setBlock(
  blockId: string,
  data: Record<string, unknown> = FULL_DATA,
  type = "taskv2",
) {
  mockNodeFixtures.set(blockId, { id: blockId, type, data });
}

describe("Taskv2BlockForm rendering (SKY-9369)", () => {
  test("returns null when the node lookup misses", () => {
    const { container } = render(<Taskv2BlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null when the node is not a workflow block", () => {
    setBlock("u1", FULL_DATA, "start");
    isWorkflowBlockNodeMock.mockReturnValue(false);
    const { container } = render(<Taskv2BlockForm blockId="u1" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null when the node is not a Taskv2 node", () => {
    setBlock("b1", FULL_DATA, "task");
    const { container } = render(<Taskv2BlockForm blockId="b1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders all inline fields seeded from node data", () => {
    setBlock("b1");

    render(<Taskv2BlockForm blockId="b1" />);

    const form = screen.getByTestId("taskv2-block-form");
    expect(form.getAttribute("data-block-id")).toBe("b1");

    expect(screen.getByText("URL")).toBeDefined();
    expect(screen.getByText("Prompt")).toBeDefined();
    expect(screen.getByText("Advanced Settings")).toBeDefined();
    expect(screen.getByDisplayValue("https://example.com")).toBeDefined();
    expect(screen.getByDisplayValue("navigate and extract")).toBeDefined();

    fireEvent.click(screen.getByText("Advanced Settings"));

    expect(screen.getByText("Max Steps")).toBeDefined();
    expect(screen.getByText("2FA Identifier")).toBeDefined();
    expect(screen.getByText("2FA Verification URL")).toBeDefined();
    expect(screen.getByTestId("model-selector")).toBeDefined();
    expect(screen.getByDisplayValue("25")).toBeDefined();
  });

  test("renders the latest node data after React Flow state updates", () => {
    setBlock("b1");
    const { rerender } = render(<Taskv2BlockForm blockId="b1" />);

    expect(screen.getByDisplayValue("https://example.com")).toBeDefined();

    setBlock("b1", {
      ...FULL_DATA,
      url: "https://latest.example.com",
      prompt: "latest prompt",
    });
    rerender(<Taskv2BlockForm blockId="b1" />);

    expect(
      screen.getByDisplayValue("https://latest.example.com"),
    ).toBeDefined();
    expect(screen.getByDisplayValue("latest prompt")).toBeDefined();
  });
});

describe("Taskv2BlockForm save behavior (SKY-9369)", () => {
  test("persists URL edits immediately via updateNodeData", () => {
    setBlock("b1");
    render(<Taskv2BlockForm blockId="b1" />);

    const urlField = screen.getByDisplayValue("https://example.com");
    fireEvent.change(urlField, { target: { value: "https://updated.com" } });

    // Containerized editors write through useUpdate on every onChange so
    // the tile and sidebar surfaces stay in sync via React Flow node data.
    expect(updateNodeDataMock).toHaveBeenCalledWith("b1", {
      url: "https://updated.com",
    });
  });

  test("persists prompt + maxSteps edits immediately via updateNodeData", () => {
    setBlock("b1");
    render(<Taskv2BlockForm blockId="b1" />);

    const promptField = screen.getByDisplayValue("navigate and extract");
    fireEvent.change(promptField, { target: { value: "new prompt" } });
    expect(updateNodeDataMock).toHaveBeenCalledWith("b1", {
      prompt: "new prompt",
    });

    fireEvent.click(screen.getByText("Advanced Settings"));
    const maxSteps = screen.getByDisplayValue("25");
    fireEvent.change(maxSteps, { target: { value: "42" } });
    expect(updateNodeDataMock).toHaveBeenCalledWith("b1", {
      maxSteps: 42,
    });
  });

  test("does not save when block is non-editable", () => {
    setBlock("b1", { ...FULL_DATA, editable: false });
    render(<Taskv2BlockForm blockId="b1" />);

    const urlField = screen.getByDisplayValue("https://example.com");
    fireEvent.change(urlField, { target: { value: "x" } });

    act(() => {
      vi.advanceTimersByTime(DEFAULT_DEBOUNCE_MS);
    });

    expect(updateNodeDataMock).not.toHaveBeenCalled();
  });

  test("output payload byte-identity: same edits produce the same updateNodeData object across two renders", () => {
    function applyEdits() {
      fireEvent.change(screen.getByDisplayValue("https://example.com"), {
        target: { value: "https://canonical.com" },
      });
      fireEvent.click(screen.getByText("Advanced Settings"));
      fireEvent.change(screen.getByDisplayValue("25"), {
        target: { value: "10" },
      });
      act(() => {
        vi.advanceTimersByTime(DEFAULT_DEBOUNCE_MS);
      });
    }

    setBlock("b1");
    const { unmount } = render(<Taskv2BlockForm blockId="b1" />);
    applyEdits();
    const firstSnapshot =
      updateNodeDataMock.mock.calls[
        updateNodeDataMock.mock.calls.length - 1
      ]![1];
    unmount();

    updateNodeDataMock.mockReset();
    mockNodeFixtures.clear();
    setBlock("b1");
    render(<Taskv2BlockForm blockId="b1" />);
    applyEdits();
    const secondSnapshot =
      updateNodeDataMock.mock.calls[
        updateNodeDataMock.mock.calls.length - 1
      ]![1];

    expect(JSON.stringify(secondSnapshot)).toBe(JSON.stringify(firstSnapshot));
  });
});

describe("Taskv2BlockForm PendingCommits integration (SKY-9369 × SKY-9362)", () => {
  test("registers a commit on mount and unregisters on unmount", () => {
    setBlock("b1");
    const { unmount } = render(<Taskv2BlockForm blockId="b1" />);

    expect(usePendingCommitsStore.getState().commits["b1"]).toBeDefined();

    unmount();

    expect(usePendingCommitsStore.getState().commits["b1"]).toBeUndefined();
  });

  test("flush() resolves cleanly because edits already persisted", () => {
    setBlock("b1");
    render(<Taskv2BlockForm blockId="b1" />);

    fireEvent.change(screen.getByDisplayValue("https://example.com"), {
      target: { value: "https://urgent.com" },
    });

    // Containerized editors write immediately, so the field is already in
    // node data before flush — flush stays a no-op for the data path but
    // returns true so switching-blocks orchestration can safely await it.
    expect(updateNodeDataMock).toHaveBeenCalledWith("b1", {
      url: "https://urgent.com",
    });

    act(() => {
      const flushed = usePendingCommitsStore.getState().flush("b1");
      expect(flushed).toBe(true);
    });
  });

  // The saved-N-sec-ago footer is exercised through the wrapper's
  // debounced-save observer in production where node.data actually changes
  // through React Flow's store. The test mocks updateNodeData without
  // mutating the fixture, so the wrapper's `value` snapshot never moves
  // and the footer tick is suppressed. Coverage for the footer lives in
  // panels/BlockConfigSidebar tests where the real store is involved.
});
