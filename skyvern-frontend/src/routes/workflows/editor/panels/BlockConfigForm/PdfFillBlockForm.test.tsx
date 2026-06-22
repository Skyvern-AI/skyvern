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

import type { PdfFillNode } from "../../nodes/PdfFillNode/types";

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

vi.mock("@/components/HelpTooltip", () => ({
  HelpTooltip: () => <span data-testid="help-tooltip" />,
}));

vi.mock("@/components/ModelSelector", () => ({
  ModelSelector: (props: {
    value: unknown;
    onChange: (value: unknown) => void;
  }) => (
    <button
      data-testid="model-selector"
      onClick={() => props.onChange({ model_name: "gpt-test" })}
    />
  ),
}));

vi.mock("@/components/WorkflowBlockInput", () => ({
  WorkflowBlockInput: (props: {
    value: string;
    onChange: (value: string) => void;
  }) => (
    <input
      data-testid="workflow-block-input"
      value={props.value}
      onChange={(event) => props.onChange(event.target.value)}
    />
  ),
}));

vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: (props: {
    value: string;
    onChange: (value: string) => void;
    placeholder?: string;
  }) => (
    <textarea
      data-testid={`wbi-ph-${props.placeholder ?? ""}`}
      value={props.value}
      onChange={(event) => props.onChange(event.target.value)}
    />
  ),
}));

vi.mock("@/routes/workflows/components/CodeEditor", () => ({
  CodeEditor: (props: { value: string; onChange: (value: string) => void }) => (
    <textarea
      data-testid="code-editor"
      value={props.value}
      onChange={(event) => props.onChange(event.target.value)}
    />
  ),
}));

vi.mock("../../nodes/HttpRequestNode/HttpUtils", () => ({
  JsonValidator: ({ value }: { value: string }) => (
    <div data-testid="json-validator" data-value={value} />
  ),
}));

vi.mock("../../nodes/TaskNode/ParametersMultiSelect", () => ({
  ParametersMultiSelect: (props: {
    parameters: Array<string>;
    onParametersChange: (next: Array<string>) => void;
  }) => (
    <button
      data-testid="parameters-change"
      onClick={() => props.onParametersChange(["param_a"])}
    />
  ),
}));

vi.mock("../../nodes/WorkflowBlockParameterSelect", () => ({
  WorkflowBlockParameterSelect: (props: {
    onAdd: (parameterKey: string) => void;
  }) => (
    <button
      data-testid="parameter-select-add"
      onClick={() => props.onAdd("applicant")}
    />
  ),
}));

vi.mock("../../nodes/IgnoreWorkflowSystemPrompt", () => ({
  IgnoreWorkflowSystemPrompt: (props: {
    onIgnoreWorkflowSystemPromptChange: (value: boolean) => void;
  }) => (
    <button
      data-testid="ignore-workflow-system-prompt"
      onClick={() => props.onIgnoreWorkflowSystemPromptChange(true)}
    />
  ),
}));

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

vi.mock("@/components/ui/popover", () => {
  const Pass = ({ children }: { children?: ReactNode }) => <>{children}</>;
  return {
    Popover: Pass,
    PopoverTrigger: Pass,
    PopoverContent: Pass,
  };
});

import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";
import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { PdfFillBlockForm } from "./PdfFillBlockForm";

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

function setPdfFillNode(
  id: string,
  overrides: Partial<PdfFillNode["data"]> = {},
) {
  mockNodes.set(id, {
    id,
    type: "pdfFill",
    data: {
      debuggable: true,
      label: "pdf_fill_1",
      continueOnFailure: false,
      editable: true,
      model: null,
      fileUrl: "",
      prompt: "",
      payload: "{}",
      llmKey: "",
      parameterKeys: [],
      ...overrides,
    },
  });
}

describe("PdfFillBlockForm", () => {
  test("returns null for missing node", () => {
    const { container } = render(<PdfFillBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null for wrong node type", () => {
    mockNodes.set("p1", { id: "p1", type: "task", data: {} });
    const { container } = render(<PdfFillBlockForm blockId="p1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders core PDF fill controls", () => {
    setPdfFillNode("p1");
    render(<PdfFillBlockForm blockId="p1" />);

    expect(screen.getByTestId("pdf-fill-block-form")).toBeDefined();
    expect(screen.getByText("File URL")).toBeDefined();
    expect(screen.getByText("Prompt")).toBeDefined();
    expect(screen.getByText("Payload")).toBeDefined();
    expect(screen.queryByText("API Key")).toBeNull();
  });

  test("editing file URL, prompt, and payload propagates", () => {
    setPdfFillNode("p1");
    render(<PdfFillBlockForm blockId="p1" />);

    const inputs = screen.getAllByTestId("workflow-block-input");
    expect(inputs[0]).toBeDefined();
    fireEvent.change(inputs[0]!, { target: { value: "{{ pdf_output }}" } });
    fireEvent.change(
      screen.getByTestId("wbi-ph-Fill the form using the payload."),
      { target: { value: "Fill the applicant form" } },
    );
    fireEvent.change(screen.getByTestId("code-editor"), {
      target: { value: '{"name":"Jane"}' },
    });

    expect(updateNodeData).toHaveBeenCalledWith("p1", {
      fileUrl: "{{ pdf_output }}",
    });
    expect(updateNodeData).toHaveBeenCalledWith("p1", {
      prompt: "Fill the applicant form",
    });
    expect(updateNodeData).toHaveBeenCalledWith("p1", {
      payload: '{"name":"Jane"}',
    });
  });

  test("registers/unregisters commit on mount/unmount", () => {
    setPdfFillNode("p1");
    const { unmount } = render(<PdfFillBlockForm blockId="p1" />);

    expect(usePendingCommitsStore.getState().commits["p1"]).toBeDefined();
    unmount();
    expect(usePendingCommitsStore.getState().commits["p1"]).toBeUndefined();
  });

  test("flush via PendingCommitsStore returns true", () => {
    setPdfFillNode("p1");
    render(<PdfFillBlockForm blockId="p1" />);

    let ok = false;
    act(() => {
      ok = usePendingCommitsStore.getState().flush("p1");
    });
    expect(ok).toBe(true);
  });
});
