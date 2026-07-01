// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const mockUpdateNodeData = vi.fn();
const mockNodeFixtures = new Map<
  string,
  { id: string; type: string; data?: Record<string, unknown> } | undefined
>();

vi.mock("@xyflow/react", async () => {
  const actual =
    await vi.importActual<typeof import("@xyflow/react")>("@xyflow/react");
  return {
    ...actual,
    useReactFlow: () => ({
      getNode: (id: string) => mockNodeFixtures.get(id),
      updateNodeData: mockUpdateNodeData,
    }),
  };
});

// Stub heavy form sub-components so the test only exercises the dispatcher
// surface + onChange wiring of FileParserBlockForm. The inline form's exact
// fields are mirrored by these data-testids.
vi.mock("@/components/WorkflowBlockInput", () => ({
  WorkflowBlockInput: ({
    value,
    onChange,
  }: {
    value: string;
    onChange: (v: string) => void;
  }) => (
    <input
      data-testid="file-url-input"
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
    }) => (
      <textarea
        data-testid="json-schema-input"
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
  }) => (
    <button
      data-testid="model-selector"
      data-value={JSON.stringify(value)}
      onClick={() => onChange({ model_name: "test-model" })}
    >
      model
    </button>
  ),
}));

vi.mock("@/components/HelpTooltip", () => ({
  HelpTooltip: () => null,
}));

vi.mock("../../helpContent", () => ({
  helpTooltips: {
    fileParser: {
      fileUrl: "url-help",
      fileType: "type-help",
    },
  },
}));

vi.mock("../../nodes", async () => {
  const actual = await vi.importActual<{
    isWorkflowBlockNode: unknown;
    AppNode: unknown;
  }>("../../nodes");
  return {
    ...actual,
    isWorkflowBlockNode: (node: { type: string }) =>
      node.type !== "start" && node.type !== "nodeAdder",
  };
});

import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";

import { FileParserBlockForm } from "./FileParserBlockForm";

const baseFileParserData = {
  debuggable: true,
  editable: true,
  label: "parse-file",
  continueOnFailure: false,
  fileUrl: "https://example.com/doc.pdf",
  fileType: "auto_detect" as const,
  jsonSchema: "null",
  model: null,
};

beforeEach(() => {
  mockUpdateNodeData.mockReset();
  mockNodeFixtures.clear();
  usePendingCommitsStore.setState({ commits: {} });
  useSidebarSaveStateStore.getState().reset();
});

afterEach(() => {
  cleanup();
});

describe("FileParserBlockForm (SKY-9381)", () => {
  test("renders nothing when the node lookup misses", () => {
    const { container } = render(<FileParserBlockForm blockId="missing" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders nothing when the resolved node is not a fileParser", () => {
    mockNodeFixtures.set("not-fp", {
      id: "not-fp",
      type: "task",
      data: baseFileParserData,
    });
    const { container } = render(<FileParserBlockForm blockId="not-fp" />);
    expect(container.firstChild).toBeNull();
  });

  test("renders all inline form fields for a valid fileParser block", () => {
    mockNodeFixtures.set("fp1", {
      id: "fp1",
      type: "fileParser",
      data: baseFileParserData,
    });
    render(<FileParserBlockForm blockId="fp1" />);

    expect(screen.getByTestId("file-parser-block-form")).toBeDefined();
    expect(screen.getByTestId("file-url-input")).toBeDefined();
    // File Type select trigger renders the label as visible text.
    expect(screen.getByText("Auto detect")).toBeDefined();
    expect(screen.getByTestId("json-schema-input")).toBeDefined();
    expect(screen.getByTestId("model-selector")).toBeDefined();
    const advancedSettings = screen.getByText("Advanced Settings");
    fireEvent.click(advancedSettings);
    expect(
      advancedSettings.compareDocumentPosition(
        screen.getByText("Ignore System Prompt"),
      ) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  test("File URL onChange dispatches updateNodeData via useUpdate (byte-identical write)", () => {
    mockNodeFixtures.set("fp1", {
      id: "fp1",
      type: "fileParser",
      data: baseFileParserData,
    });
    render(<FileParserBlockForm blockId="fp1" />);

    const input = screen.getByTestId("file-url-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "https://example.com/x.csv" } });

    // CSV extension is auto-inferred from the URL: fileType "auto_detect"
    // (the default placeholder) is treated as inference-friendly so the
    // sidebar sets it to the detected type alongside the URL write.
    expect(mockUpdateNodeData).toHaveBeenCalledWith("fp1", {
      fileUrl: "https://example.com/x.csv",
      fileType: "csv",
    });
  });

  test("File URL onChange does not auto-infer when fileType is already explicitly set to a different type", () => {
    mockNodeFixtures.set("fp1", {
      id: "fp1",
      type: "fileParser",
      data: { ...baseFileParserData, fileType: "pdf" as const },
    });
    render(<FileParserBlockForm blockId="fp1" />);

    const input = screen.getByTestId("file-url-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "https://example.com/x.csv" } });

    // fileType stays unchanged because user already chose pdf.
    expect(mockUpdateNodeData).toHaveBeenCalledWith("fp1", {
      fileUrl: "https://example.com/x.csv",
    });
  });

  test("does not call updateNodeData when block is not editable", () => {
    mockNodeFixtures.set("fp1", {
      id: "fp1",
      type: "fileParser",
      data: { ...baseFileParserData, editable: false },
    });
    render(<FileParserBlockForm blockId="fp1" />);

    const input = screen.getByTestId("file-url-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "https://example.com/x.csv" } });

    expect(mockUpdateNodeData).not.toHaveBeenCalled();
  });

  test("registers a commit with PendingCommitsStore on mount; unregisters on unmount", () => {
    mockNodeFixtures.set("fp1", {
      id: "fp1",
      type: "fileParser",
      data: baseFileParserData,
    });
    const { unmount } = render(<FileParserBlockForm blockId="fp1" />);
    expect(typeof usePendingCommitsStore.getState().commits["fp1"]).toBe(
      "function",
    );
    unmount();
    expect(usePendingCommitsStore.getState().commits["fp1"]).toBeUndefined();
  });

  test("flushing the registered commit returns true when there are no pending edits", () => {
    // useDebouncedSidebarSave.commit() short-circuits when value matches the
    // baseline, so a flush right after mount returns ok=true without
    // bumping lastUpdatedAt.
    mockNodeFixtures.set("fp1", {
      id: "fp1",
      type: "fileParser",
      data: baseFileParserData,
    });
    render(<FileParserBlockForm blockId="fp1" />);

    expect(
      useSidebarSaveStateStore.getState().getLastUpdatedAt("fp1"),
    ).toBeNull();

    const ok = usePendingCommitsStore.getState().flush("fp1");
    expect(ok).toBe(true);
  });
});
