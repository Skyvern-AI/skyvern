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
      data-testid="wfb-input"
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
      exampleValue: string;
      suggestionContext: unknown;
    }) => (
      <textarea
        data-testid="schema-input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    ),
  }),
);

vi.mock("@/components/ModelSelector", () => ({
  ModelSelector: ({
    value,
    onChange,
  }: {
    value: unknown;
    onChange: (v: unknown) => void;
    className?: string;
  }) => (
    <select
      data-testid="model-select"
      value={(value as string) ?? ""}
      onChange={(e) => onChange(e.target.value || null)}
    >
      <option value="">—</option>
      <option value="gpt-4o">gpt-4o</option>
    </select>
  ),
}));

import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";
import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { PDFParserBlockForm } from "./PDFParserBlockForm";

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

function setPDFParserNode(
  id: string,
  data: Partial<{
    fileUrl: string;
    jsonSchema: string;
    model: unknown;
    editable: boolean;
  }> = {},
) {
  mockNodes.set(id, {
    id,
    type: "pdfParser",
    data: {
      fileUrl: data.fileUrl ?? "",
      jsonSchema: data.jsonSchema ?? "null",
      model: data.model ?? null,
      editable: data.editable ?? true,
      label: "block_1",
      continueOnFailure: false,
      debuggable: true,
    },
  });
}

describe("PDFParserBlockForm (SKY-93XX)", () => {
  test("returns null for missing node", () => {
    const { container } = render(<PDFParserBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("returns null for wrong node type", () => {
    mockNodes.set("p1", { id: "p1", type: "task", data: {} });
    const { container } = render(<PDFParserBlockForm blockId="p1" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders fileUrl, jsonSchema, and model with current node data", () => {
    setPDFParserNode("p1", {
      fileUrl: "https://x.pdf",
      jsonSchema: '{"a":1}',
      model: "gpt-4o",
    });
    render(<PDFParserBlockForm blockId="p1" />);
    expect((screen.getByTestId("wfb-input") as HTMLInputElement).value).toBe(
      "https://x.pdf",
    );
    expect(
      (screen.getByTestId("schema-input") as HTMLTextAreaElement).value,
    ).toBe('{"a":1}');
    expect(
      (screen.getByTestId("model-select") as HTMLSelectElement).value,
    ).toBe("gpt-4o");
  });

  test("editing fileUrl propagates via updateNodeData", () => {
    setPDFParserNode("p1");
    render(<PDFParserBlockForm blockId="p1" />);
    fireEvent.change(screen.getByTestId("wfb-input"), {
      target: { value: "https://new.pdf" },
    });
    expect(updateNodeData).toHaveBeenCalledWith("p1", {
      fileUrl: "https://new.pdf",
    });
  });

  test("editing jsonSchema propagates via updateNodeData", () => {
    setPDFParserNode("p1");
    render(<PDFParserBlockForm blockId="p1" />);
    fireEvent.change(screen.getByTestId("schema-input"), {
      target: { value: '{"k":2}' },
    });
    expect(updateNodeData).toHaveBeenCalledWith("p1", {
      jsonSchema: '{"k":2}',
    });
  });

  test("editing model propagates via updateNodeData", () => {
    setPDFParserNode("p1");
    render(<PDFParserBlockForm blockId="p1" />);
    fireEvent.change(screen.getByTestId("model-select"), {
      target: { value: "gpt-4o" },
    });
    expect(updateNodeData).toHaveBeenCalledWith("p1", { model: "gpt-4o" });
  });

  test("non-editable: edits do not propagate", () => {
    setPDFParserNode("p1", { editable: false });
    render(<PDFParserBlockForm blockId="p1" />);
    fireEvent.change(screen.getByTestId("wfb-input"), {
      target: { value: "x" },
    });
    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("registers/unregisters commit", () => {
    setPDFParserNode("p1");
    const { unmount } = render(<PDFParserBlockForm blockId="p1" />);
    expect(usePendingCommitsStore.getState().commits["p1"]).toBeDefined();
    unmount();
    expect(usePendingCommitsStore.getState().commits["p1"]).toBeUndefined();
  });

  test("flush via PendingCommitsStore returns true", () => {
    setPDFParserNode("p1");
    render(<PDFParserBlockForm blockId="p1" />);
    let ok = false;
    act(() => {
      ok = usePendingCommitsStore.getState().flush("p1");
    });
    expect(ok).toBe(true);
  });
});
