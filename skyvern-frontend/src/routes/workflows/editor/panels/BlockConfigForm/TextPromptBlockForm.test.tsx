// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import {
  textPromptNodeDefaultData,
  type TextPromptNodeData,
} from "@/routes/workflows/editor/nodes/TextPromptNode/types";
import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";

// Stub heavy field components so the form's contract — controlled state,
// debounced save, register/unregister — is what's under test, not the
// internals of CodeEditor / SelectTrigger / parameter autocomplete.
vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: ({
    value,
    onChange,
  }: {
    value: string;
    onChange: (v: string) => void;
    [key: string]: unknown;
  }) => (
    <textarea
      data-testid="prompt-textarea"
      value={value}
      onChange={(e) => onChange(e.target.value)}
    />
  ),
}));

vi.mock("@/components/ModelSelector", () => ({
  ModelSelector: ({
    value,
    onChange,
  }: {
    value: { model_name: string } | null;
    onChange: (v: { model_name: string } | null) => void;
    [key: string]: unknown;
  }) => (
    <button
      type="button"
      data-testid="model-selector"
      data-value={value?.model_name ?? ""}
      onClick={() => onChange({ model_name: "test-model" })}
    >
      model
    </button>
  ),
}));

vi.mock(
  "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup",
  () => ({
    WorkflowDataSchemaInputGroup: ({
      value,
      onChange,
    }: {
      value: string;
      onChange: (v: string) => void;
      [key: string]: unknown;
    }) => (
      <textarea
        data-testid="schema-textarea"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    ),
  }),
);

const updateNodeDataMock = vi.fn();
const mockNodeFixtures = new Map<
  string,
  { id: string; type: string; data: TextPromptNodeData } | undefined
>();

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useReactFlow: () => ({
      getNode: (id: string) => mockNodeFixtures.get(id),
      updateNodeData: updateNodeDataMock,
    }),
  };
});

import { TextPromptBlockForm } from "./TextPromptBlockForm";

function makeNode(
  id: string,
  overrides: Partial<TextPromptNodeData> = {},
): { id: string; type: "textPrompt"; data: TextPromptNodeData } {
  return {
    id,
    type: "textPrompt",
    data: { ...textPromptNodeDefaultData, ...overrides },
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  updateNodeDataMock.mockReset();
  mockNodeFixtures.clear();
  usePendingCommitsStore.setState({ commits: {} });
  useSidebarSaveStateStore.getState().reset();
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

describe("TextPromptBlockForm (SKY-9378)", () => {
  test("renders nothing when the node lookup misses", () => {
    mockNodeFixtures.set("missing", undefined);
    const { container } = render(<TextPromptBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders nothing when the node is not a TextPrompt", () => {
    mockNodeFixtures.set("not-text", {
      id: "not-text",
      // Force a non-textPrompt shape past the partial type guard.
      type: "task",
    } as never);
    const { container } = render(<TextPromptBlockForm blockId="not-text" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders the prompt, model, and data-schema fields seeded from node data", () => {
    mockNodeFixtures.set(
      "b1",
      makeNode("b1", {
        prompt: "what is the answer?",
        jsonSchema: '{"type":"object"}',
        model: { model_name: "gpt-4" },
      }),
    );

    render(<TextPromptBlockForm blockId="b1" />);

    expect(
      (screen.getByTestId("prompt-textarea") as HTMLTextAreaElement).value,
    ).toBe("what is the answer?");
    expect(
      (screen.getByTestId("schema-textarea") as HTMLTextAreaElement).value,
    ).toBe('{"type":"object"}');
    expect(
      screen.getByTestId("model-selector").getAttribute("data-value"),
    ).toBe("gpt-4");
  });

  test("persists prompt edits immediately via updateNodeData", () => {
    mockNodeFixtures.set("b1", makeNode("b1", { prompt: "old" }));

    render(<TextPromptBlockForm blockId="b1" />);

    fireEvent.change(screen.getByTestId("prompt-textarea"), {
      target: { value: "new" },
    });

    // Containerized editors write through useUpdate on every onChange so the
    // tile and sidebar surfaces stay in sync via React Flow node data.
    expect(updateNodeDataMock).toHaveBeenCalledWith(
      "b1",
      expect.objectContaining({ prompt: "new" }),
    );
  });

  test("persists data-schema edits immediately via updateNodeData", () => {
    mockNodeFixtures.set("b1", makeNode("b1"));

    render(<TextPromptBlockForm blockId="b1" />);

    fireEvent.change(screen.getByTestId("schema-textarea"), {
      target: { value: '{"new":true}' },
    });

    expect(updateNodeDataMock).toHaveBeenCalledWith(
      "b1",
      expect.objectContaining({ jsonSchema: '{"new":true}' }),
    );
  });

  test("persists model selection immediately via updateNodeData", () => {
    mockNodeFixtures.set("b1", makeNode("b1", { model: null }));

    render(<TextPromptBlockForm blockId="b1" />);

    fireEvent.click(screen.getByTestId("model-selector"));

    expect(updateNodeDataMock).toHaveBeenCalledWith(
      "b1",
      expect.objectContaining({ model: { model_name: "test-model" } }),
    );
  });

  test("does not write when editable is false (parity with useUpdate)", () => {
    mockNodeFixtures.set("b1", makeNode("b1", { editable: false }));

    render(<TextPromptBlockForm blockId="b1" />);

    fireEvent.change(screen.getByTestId("prompt-textarea"), {
      target: { value: "ignored" },
    });

    expect(updateNodeDataMock).not.toHaveBeenCalled();
  });

  test("registers a commit on mount and unregisters on unmount", () => {
    mockNodeFixtures.set("b1", makeNode("b1"));

    const { unmount } = render(<TextPromptBlockForm blockId="b1" />);

    expect(usePendingCommitsStore.getState().commits["b1"]).toBeDefined();

    unmount();

    expect(usePendingCommitsStore.getState().commits["b1"]).toBeUndefined();
  });

  test("flush() resolves cleanly because edits already persisted", () => {
    mockNodeFixtures.set("b1", makeNode("b1", { prompt: "" }));

    render(<TextPromptBlockForm blockId="b1" />);

    fireEvent.change(screen.getByTestId("prompt-textarea"), {
      target: { value: "in-flight" },
    });
    // Containerized editors write immediately, so the field is already in
    // node data before flush — flush is a no-op for the data path but still
    // returns true so the dispatcher's switching-blocks orchestration can
    // safely await it.
    expect(updateNodeDataMock).toHaveBeenCalledWith(
      "b1",
      expect.objectContaining({ prompt: "in-flight" }),
    );

    let flushed: boolean | undefined;
    act(() => {
      flushed = usePendingCommitsStore.getState().flush("b1");
    });
    expect(flushed).toBe(true);
  });
});
