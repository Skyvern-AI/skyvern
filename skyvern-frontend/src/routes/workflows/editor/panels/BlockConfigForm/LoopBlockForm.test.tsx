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
    useNodesData: (id: string) => {
      const node = mockNodes.get(id);
      return node ? { id: node.id, type: node.type, data: node.data } : null;
    },
  };
});

vi.mock("@/components/WorkflowBlockInput", () => ({
  WorkflowBlockInput: ({
    value,
    onChange,
  }: {
    value: string;
    onChange: (v: string) => void;
    nodeId: string;
    className?: string;
  }) => (
    <input
      data-testid="loop-variable-input"
      value={value}
      onChange={(e) => onChange(e.target.value)}
    />
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
      exampleValue: unknown;
      suggestionContext: unknown;
      helpTooltip?: string;
    }) => (
      <textarea
        data-testid="data-schema-input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    ),
  }),
);

// jsdom + Radix Checkbox can be flaky for click→onCheckedChange. Replace with
// a native checkbox so the test only exercises the form's onChange wiring.
vi.mock("@/components/ui/checkbox", () => ({
  Checkbox: ({
    checked,
    disabled,
    onCheckedChange,
    ...rest
  }: {
    checked?: boolean | "indeterminate";
    disabled?: boolean;
    onCheckedChange?: (checked: boolean | "indeterminate") => void;
    [key: string]: unknown;
  }) => (
    <input
      type="checkbox"
      checked={!!checked}
      disabled={disabled}
      onChange={(e) => onCheckedChange?.(e.target.checked)}
      {...rest}
    />
  ),
}));

import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";
import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { LoopBlockForm } from "./LoopBlockForm";

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

function setLoopNode(
  id: string,
  data: Partial<{
    loopVariableReference: string;
    dataSchema: string;
    completeIfEmpty: boolean;
    continueOnFailure: boolean;
    nextLoopOnFailure: boolean | undefined;
    editable: boolean;
  }> = {},
) {
  mockNodes.set(id, {
    id,
    type: "loop",
    data: {
      loopVariableReference: data.loopVariableReference ?? "",
      dataSchema: data.dataSchema ?? "null",
      completeIfEmpty: data.completeIfEmpty ?? false,
      continueOnFailure: data.continueOnFailure ?? false,
      nextLoopOnFailure:
        "nextLoopOnFailure" in data ? data.nextLoopOnFailure : false,
      editable: data.editable ?? true,
      label: "block_1",
      loopValue: "",
      debuggable: true,
      model: null,
      // SKY-8771 introduced loopKind / whileCondition* fields. Default the
      // fixture to the for-each branch so the existing test cases continue
      // to exercise the for-each form fields.
      loopKind: "for_each",
      whileConditionExpression: "{{ true }}",
      whileConditionCriteriaType: "jinja2_template",
    },
  });
}

describe("LoopBlockForm (SKY-9361)", () => {
  test("returns null for missing node", () => {
    const { container } = render(<LoopBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null for wrong node type", () => {
    mockNodes.set("p1", { id: "p1", type: "task", data: {} });
    const { container } = render(<LoopBlockForm blockId="p1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders all 5 fields with current node data", () => {
    setLoopNode("p1", {
      loopVariableReference: "{{ items }}",
      dataSchema: '{"x":1}',
      completeIfEmpty: true,
      continueOnFailure: false,
      nextLoopOnFailure: true,
    });
    render(<LoopBlockForm blockId="p1" />);

    expect(screen.getByText("Loop Value")).toBeDefined();
    expect(screen.getByText("Continue if Empty")).toBeDefined();
    expect(screen.getByText("Continue Workflow if Loop Fails")).toBeDefined();
    expect(screen.getByText("Skip Iterations that Fail")).toBeDefined();

    expect(
      (screen.getByTestId("loop-variable-input") as HTMLInputElement).value,
    ).toBe("{{ items }}");
    expect(
      (screen.getByTestId("data-schema-input") as HTMLTextAreaElement).value,
    ).toBe('{"x":1}');

    const completeIfEmpty = screen.getByTestId(
      "checkbox-completeIfEmpty",
    ) as HTMLInputElement;
    const continueOnFailure = screen.getByTestId(
      "checkbox-continueOnFailure",
    ) as HTMLInputElement;
    const nextLoopOnFailure = screen.getByTestId(
      "checkbox-nextLoopOnFailure",
    ) as HTMLInputElement;
    expect(completeIfEmpty.checked).toBe(true);
    expect(continueOnFailure.checked).toBe(false);
    expect(nextLoopOnFailure.checked).toBe(true);
  });

  test("editing loopVariableReference propagates via updateNodeData", () => {
    setLoopNode("p1");
    render(<LoopBlockForm blockId="p1" />);
    fireEvent.change(screen.getByTestId("loop-variable-input"), {
      target: { value: "{{ items }}" },
    });
    expect(updateNodeData).toHaveBeenCalledWith("p1", {
      loopVariableReference: "{{ items }}",
    });
  });

  test("editing dataSchema propagates via updateNodeData", () => {
    setLoopNode("p1");
    render(<LoopBlockForm blockId="p1" />);
    fireEvent.change(screen.getByTestId("data-schema-input"), {
      target: { value: '{"k":2}' },
    });
    expect(updateNodeData).toHaveBeenCalledWith("p1", {
      dataSchema: '{"k":2}',
    });
  });

  test("toggling completeIfEmpty propagates (true → false)", () => {
    setLoopNode("p1", { completeIfEmpty: true });
    render(<LoopBlockForm blockId="p1" />);
    fireEvent.click(screen.getByTestId("checkbox-completeIfEmpty"));
    expect(updateNodeData).toHaveBeenCalledWith("p1", {
      completeIfEmpty: false,
    });
  });

  test("toggling continueOnFailure propagates", () => {
    setLoopNode("p1", { continueOnFailure: false });
    render(<LoopBlockForm blockId="p1" />);
    fireEvent.click(screen.getByTestId("checkbox-continueOnFailure"));
    expect(updateNodeData).toHaveBeenCalledWith("p1", {
      continueOnFailure: true,
    });
  });

  test("toggling nextLoopOnFailure (defaulting from undefined → true) propagates", () => {
    setLoopNode("p1", { nextLoopOnFailure: undefined });
    render(<LoopBlockForm blockId="p1" />);
    const cb = screen.getByTestId(
      "checkbox-nextLoopOnFailure",
    ) as HTMLInputElement;
    expect(cb.checked).toBe(false);
    fireEvent.click(cb);
    expect(updateNodeData).toHaveBeenCalledWith("p1", {
      nextLoopOnFailure: true,
    });
  });

  test("non-editable: WorkflowBlockInput edits don't propagate", () => {
    setLoopNode("p1", { editable: false });
    render(<LoopBlockForm blockId="p1" />);
    fireEvent.change(screen.getByTestId("loop-variable-input"), {
      target: { value: "{{ items }}" },
    });
    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("non-editable: Checkbox toggles don't propagate", () => {
    setLoopNode("p1", { editable: false });
    render(<LoopBlockForm blockId="p1" />);
    fireEvent.click(screen.getByTestId("checkbox-completeIfEmpty"));
    fireEvent.click(screen.getByTestId("checkbox-continueOnFailure"));
    fireEvent.click(screen.getByTestId("checkbox-nextLoopOnFailure"));
    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("registers/unregisters commit", () => {
    setLoopNode("p1");
    const { unmount } = render(<LoopBlockForm blockId="p1" />);
    expect(usePendingCommitsStore.getState().commits["p1"]).toBeDefined();
    unmount();
    expect(usePendingCommitsStore.getState().commits["p1"]).toBeUndefined();
  });

  test("flush via PendingCommitsStore returns true", () => {
    setLoopNode("p1");
    render(<LoopBlockForm blockId="p1" />);
    let ok = false;
    act(() => {
      ok = usePendingCommitsStore.getState().flush("p1");
    });
    expect(ok).toBe(true);
  });
});
