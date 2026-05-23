// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type { ValidationNode } from "../../nodes/ValidationNode/types";

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
    useNodes: () => Array.from(mockNodes.values()),
    useEdges: () => [],
  };
});

vi.mock("../../workflowEditorUtils", () => ({
  getAvailableOutputParameterKeys: () => [],
  isNodeInsideForLoop: () => false,
  getParentLoopSkipsOnFail: () => false,
}));

// Stub heavy components: WorkflowBlockInputTextarea, ParametersMultiSelect,
// ErrorCodeMappingEditor, ModelSelector, BlockExecutionOptions, DisableCache.
// Each is replaced with a minimal stub that exposes the props the form passes
// so we can drive the form's contract without spinning up the full editor.
vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: (props: {
    value: string;
    onChange: (value: string) => void;
    aiImprove?: { useCase?: string };
  }) => {
    // The useCase string is `workflow_editor.<block>.<field>` so we can
    // route assertions by field without needing field-specific mocks.
    const field = props.aiImprove?.useCase?.split(".").pop() ?? "unknown";
    return (
      <textarea
        data-testid={`wbi-${field}`}
        value={props.value}
        onChange={(event) => props.onChange(event.target.value)}
      />
    );
  },
}));

vi.mock("@/components/ModelSelector", () => ({
  ModelSelector: (props: {
    value: unknown;
    onChange: (value: string) => void;
  }) => (
    <select
      data-testid="model-selector"
      value={props.value === null ? "" : String(props.value)}
      onChange={(event) => props.onChange(event.target.value)}
    >
      <option value="">none</option>
      <option value="model-a">model-a</option>
    </select>
  ),
}));

vi.mock("@/components/HelpTooltip", () => ({
  HelpTooltip: () => <span data-testid="help-tooltip" />,
}));

vi.mock("../../nodes/TaskNode/ParametersMultiSelect", () => ({
  ParametersMultiSelect: (props: {
    parameters: Array<string>;
    onParametersChange: (next: Array<string>) => void;
    availableOutputParameters: Array<string>;
  }) => (
    <select
      data-testid="parameters-multi-select"
      multiple
      value={props.parameters}
      onChange={(event) =>
        props.onParametersChange(
          Array.from(event.target.selectedOptions, (o) => o.value),
        )
      }
    >
      <option value="param_a">param_a</option>
      <option value="param_b">param_b</option>
    </select>
  ),
}));

vi.mock("../../nodes/components/BlockExecutionOptions", () => ({
  BlockExecutionOptions: (props: {
    continueOnFailure: boolean;
    nextLoopOnFailure?: boolean;
    blockType: string;
    isInsideForLoop: boolean;
    onContinueOnFailureChange: (checked: boolean) => void;
    onNextLoopOnFailureChange: (checked: boolean) => void;
  }) => (
    <div
      data-testid="block-execution-options"
      data-continue={String(props.continueOnFailure)}
      data-block-type={props.blockType}
      data-inside-loop={String(props.isInsideForLoop)}
    >
      <button
        data-testid="continue-on-failure-toggle"
        onClick={() =>
          props.onContinueOnFailureChange(!props.continueOnFailure)
        }
      />
    </div>
  ),
}));

vi.mock("../../nodes/DisableCache", () => ({
  DisableCache: (props: {
    disableCache: boolean;
    editable: boolean;
    onDisableCacheChange: (next: boolean) => void;
  }) => (
    <button
      data-testid="disable-cache-toggle"
      data-disabled={String(props.disableCache)}
      onClick={() => props.onDisableCacheChange(!props.disableCache)}
    />
  ),
}));

vi.mock("../../ErrorCodeMappingEditor", () => ({
  ErrorCodeMappingEditor: (props: {
    value: string;
    onChange: (value: string) => void;
    readOnly?: boolean;
  }) => (
    <textarea
      data-testid="error-code-mapping-editor"
      value={props.value}
      readOnly={props.readOnly}
      onChange={(event) => props.onChange(event.target.value)}
    />
  ),
}));

vi.mock(
  "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup",
  () => ({
    WorkflowDataSchemaInputGroup: (props: {
      value: string;
      onChange: (value: string) => void;
    }) => (
      <textarea
        data-testid="data-schema-input"
        value={props.value}
        onChange={(event) => props.onChange(event.target.value)}
      />
    ),
  }),
);

vi.mock("../../hooks/useIsFirstNodeInWorkflow", () => ({
  useIsFirstBlockInWorkflow: () => false,
}));

// shadcn Switch is a Radix primitive that depends on PointerEvent semantics
// not fully simulated in jsdom. The form only consumes `checked` +
// `onCheckedChange`, so a button is enough to exercise the toggle path.
vi.mock("@/components/ui/switch", () => ({
  Switch: (props: {
    "data-testid"?: string;
    checked: boolean;
    onCheckedChange: (checked: boolean) => void;
    disabled?: boolean;
  }) => (
    <button
      role="switch"
      aria-checked={props.checked}
      data-testid={props["data-testid"] ?? "error-code-mapping-switch"}
      disabled={props.disabled}
      onClick={() => props.onCheckedChange(!props.checked)}
    />
  ),
}));

// Force the Accordion content to always render so we can test advanced
// settings without needing to click the trigger. Mirrors the strategy from
// the LoginBlockForm test where Radix collapses children when closed.
vi.mock("@/components/ui/accordion", () => {
  const Pass = ({ children }: { children?: ReactNode }) => <>{children}</>;
  return {
    Accordion: Pass,
    AccordionItem: Pass,
    AccordionTrigger: ({ children }: { children?: ReactNode }) => (
      <button data-testid="accordion-trigger">{children}</button>
    ),
    AccordionContent: Pass,
  };
});

import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";
import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { errorMappingExampleValue } from "../../nodes/types";
import { ValidationBlockForm } from "./ValidationBlockForm";

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

function setValidationNode(
  id: string,
  overrides: Partial<ValidationNode["data"]> = {},
) {
  mockNodes.set(id, {
    id,
    type: "validation",
    data: {
      debuggable: true,
      label: "validation_1",
      completeCriterion: "",
      terminateCriterion: "",
      navigationGoal: "",
      dataExtractionGoal: "",
      dataSchema: "null",
      errorCodeMapping: "null",
      continueOnFailure: false,
      editable: true,
      parameterKeys: [],
      disableCache: false,
      model: null,
      ...overrides,
    },
  });
}

describe("ValidationBlockForm (SKY-9361)", () => {
  test("returns null for missing node", () => {
    const { container } = render(<ValidationBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null for wrong node type", () => {
    mockNodes.set("v1", { id: "v1", type: "task", data: {} });
    const { container } = render(<ValidationBlockForm blockId="v1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders both top-level textareas with current values", () => {
    setValidationNode("v1", {
      completeCriterion: "the page shows success",
      terminateCriterion: "the page shows an error",
    });
    render(<ValidationBlockForm blockId="v1" />);

    const completeTextarea = screen.getByTestId(
      "wbi-complete_criterion",
    ) as HTMLTextAreaElement;
    const terminateTextarea = screen.getByTestId(
      "wbi-terminate_criterion",
    ) as HTMLTextAreaElement;
    expect(completeTextarea.value).toBe("the page shows success");
    expect(terminateTextarea.value).toBe("the page shows an error");
    expect(screen.getByText("Complete if...")).toBeDefined();
    expect(screen.getByText("Terminate if...")).toBeDefined();
  });

  test("editing completeCriterion propagates", () => {
    setValidationNode("v1");
    render(<ValidationBlockForm blockId="v1" />);

    fireEvent.change(screen.getByTestId("wbi-complete_criterion"), {
      target: { value: "ready" },
    });

    expect(updateNodeData).toHaveBeenCalledWith("v1", {
      completeCriterion: "ready",
    });
  });

  test("editing terminateCriterion propagates", () => {
    setValidationNode("v1");
    render(<ValidationBlockForm blockId="v1" />);

    fireEvent.change(screen.getByTestId("wbi-terminate_criterion"), {
      target: { value: "abort" },
    });

    expect(updateNodeData).toHaveBeenCalledWith("v1", {
      terminateCriterion: "abort",
    });
  });

  test("toggling errorCodeMapping switch on inserts the example JSON", () => {
    setValidationNode("v1", { errorCodeMapping: "null" });
    render(<ValidationBlockForm blockId="v1" />);

    fireEvent.click(screen.getByTestId("error-code-mapping-switch"));

    expect(updateNodeData).toHaveBeenCalledWith("v1", {
      errorCodeMapping: JSON.stringify(errorMappingExampleValue, null, 2),
    });
  });

  test("toggling errorCodeMapping switch off restores 'null'", () => {
    setValidationNode("v1", {
      errorCodeMapping: JSON.stringify(errorMappingExampleValue, null, 2),
    });
    render(<ValidationBlockForm blockId="v1" />);

    fireEvent.click(screen.getByTestId("error-code-mapping-switch"));

    expect(updateNodeData).toHaveBeenCalledWith("v1", {
      errorCodeMapping: "null",
    });
  });

  test("editing errorCodeMapping (when filled) propagates", () => {
    setValidationNode("v1", {
      errorCodeMapping: JSON.stringify(errorMappingExampleValue, null, 2),
    });
    render(<ValidationBlockForm blockId="v1" />);

    fireEvent.change(screen.getByTestId("error-code-mapping-editor"), {
      target: { value: '{"oops":"oops"}' },
    });

    expect(updateNodeData).toHaveBeenCalledWith("v1", {
      errorCodeMapping: '{"oops":"oops"}',
    });
  });

  test("toggling disableCache propagates", () => {
    setValidationNode("v1", { disableCache: false });
    render(<ValidationBlockForm blockId="v1" />);

    fireEvent.click(screen.getByTestId("disable-cache-toggle"));

    expect(updateNodeData).toHaveBeenCalledWith("v1", { disableCache: true });
  });

  test("non-editable: edits do not propagate", () => {
    setValidationNode("v1", { editable: false });
    render(<ValidationBlockForm blockId="v1" />);

    fireEvent.change(screen.getByTestId("wbi-complete_criterion"), {
      target: { value: "blocked" },
    });
    fireEvent.click(screen.getByTestId("disable-cache-toggle"));
    fireEvent.click(screen.getByTestId("error-code-mapping-switch"));

    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("registers/unregisters commit", () => {
    setValidationNode("v1");
    const { unmount } = render(<ValidationBlockForm blockId="v1" />);
    expect(usePendingCommitsStore.getState().commits["v1"]).toBeDefined();
    unmount();
    expect(usePendingCommitsStore.getState().commits["v1"]).toBeUndefined();
  });

  test("flush via PendingCommitsStore returns true", () => {
    setValidationNode("v1");
    render(<ValidationBlockForm blockId="v1" />);

    let ok = false;
    act(() => {
      ok = usePendingCommitsStore.getState().flush("v1");
    });
    expect(ok).toBe(true);
  });
});
