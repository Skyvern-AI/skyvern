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

import type { SplitPdfNode } from "../../nodes/SplitPdfNode/types";

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

import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";
import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { SplitPdfBlockForm } from "./SplitPdfBlockForm";

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

function setSplitPdfNode(
  id: string,
  overrides: Partial<SplitPdfNode["data"]> = {},
) {
  mockNodes.set(id, {
    id,
    type: "splitPdf",
    data: {
      debuggable: true,
      label: "split_pdf_1",
      continueOnFailure: false,
      editable: true,
      model: null,
      fileUrl: "",
      prompt: "",
      llmKey: "",
      parameterKeys: [],
      ...overrides,
    },
  });
}

describe("SplitPdfBlockForm", () => {
  test("returns null for missing node", () => {
    const { container } = render(<SplitPdfBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null for wrong node type", () => {
    mockNodes.set("s1", { id: "s1", type: "task", data: {} });
    const { container } = render(<SplitPdfBlockForm blockId="s1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders core Split PDF controls without payload", () => {
    setSplitPdfNode("s1");
    render(<SplitPdfBlockForm blockId="s1" />);

    expect(screen.getByTestId("split-pdf-block-form")).toBeDefined();
    expect(screen.getByText("File URL")).toBeDefined();
    expect(screen.getByText("Prompt")).toBeDefined();
    expect(screen.queryByText("Payload")).toBeNull();
    expect(screen.queryByText("API Key")).toBeNull();
    expect(
      screen
        .getByText("Advanced Settings")
        .compareDocumentPosition(
          screen.getByTestId("ignore-workflow-system-prompt"),
        ) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  test("editing file URL, prompt, and LLM key propagates", () => {
    setSplitPdfNode("s1");
    render(<SplitPdfBlockForm blockId="s1" />);

    const inputs = screen.getAllByTestId("workflow-block-input");
    expect(inputs[0]).toBeDefined();
    expect(inputs[1]).toBeDefined();
    fireEvent.change(inputs[0]!, { target: { value: "{{ pdf_output }}" } });
    fireEvent.change(
      screen.getByTestId("wbi-ph-Split the PDF into one file per document."),
      { target: { value: "Split into one file per invoice" } },
    );
    fireEvent.change(inputs[1]!, { target: { value: "gpt-4.1" } });

    expect(updateNodeData).toHaveBeenCalledWith("s1", {
      fileUrl: "{{ pdf_output }}",
    });
    expect(updateNodeData).toHaveBeenCalledWith("s1", {
      prompt: "Split into one file per invoice",
    });
    expect(updateNodeData).toHaveBeenCalledWith("s1", {
      llmKey: "gpt-4.1",
    });
  });

  test("registers/unregisters commit on mount/unmount", () => {
    setSplitPdfNode("s1");
    const { unmount } = render(<SplitPdfBlockForm blockId="s1" />);

    expect(usePendingCommitsStore.getState().commits["s1"]).toBeDefined();
    unmount();
    expect(usePendingCommitsStore.getState().commits["s1"]).toBeUndefined();
  });

  test("flush via PendingCommitsStore returns true", () => {
    setSplitPdfNode("s1");
    render(<SplitPdfBlockForm blockId="s1" />);

    let ok = false;
    act(() => {
      ok = usePendingCommitsStore.getState().flush("s1");
    });
    expect(ok).toBe(true);
  });
});
