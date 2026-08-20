// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const mockUpdateNodeData = vi.fn();
const mockNodeFixtures = new Map<
  string,
  | {
      id: string;
      type: string;
      parentId?: string;
      data?: Record<string, unknown>;
    }
  | undefined
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
    useNodes: () => Array.from(mockNodeFixtures.values()),
  };
});

// Stub the shared execution-options control so the test asserts the props the
// File Parser form feeds it and the writes its callbacks produce, rather than
// driving a Radix Select through jsdom.
vi.mock("../../nodes/components/BlockExecutionOptions", () => ({
  BlockExecutionOptions: (props: {
    continueOnFailure: boolean;
    nextLoopOnFailure?: boolean;
    blockType: string;
    editable: boolean;
    isInsideForLoop: boolean;
    parentLoopSkipsOnFail?: boolean;
    onContinueOnFailureChange: (checked: boolean) => void;
    onNextLoopOnFailureChange: (checked: boolean) => void;
  }) => (
    <div
      data-testid="block-execution-options"
      data-continue={String(props.continueOnFailure)}
      data-next-loop={String(props.nextLoopOnFailure)}
      data-block-type={props.blockType}
      data-editable={String(props.editable)}
      data-inside-loop={String(props.isInsideForLoop)}
      data-parent-skips={String(props.parentLoopSkipsOnFail)}
    >
      <button
        data-testid="continue-on-failure-toggle"
        onClick={() => {
          props.onContinueOnFailureChange(true);
          props.onNextLoopOnFailureChange(false);
        }}
      />
      <button
        data-testid="next-loop-on-failure-toggle"
        onClick={() => {
          props.onContinueOnFailureChange(false);
          props.onNextLoopOnFailureChange(true);
        }}
      />
    </div>
  ),
}));

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
  nextLoopOnFailure: false,
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

function openAdvancedSettings() {
  fireEvent.click(screen.getByText("Advanced Settings"));
}

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

  test("shows the ZIP output hint without hiding the schema input", () => {
    mockNodeFixtures.set("fp1", {
      id: "fp1",
      type: "fileParser",
      data: { ...baseFileParserData, fileType: "zip" as const },
    });
    render(<FileParserBlockForm blockId="fp1" />);

    expect(
      screen.getByText(/Data Schema is ignored for ZIP archives/),
    ).toBeDefined();
    expect(screen.getByTestId("json-schema-input")).toBeDefined();
  });

  test.each(["csv", "auto_detect"] as const)(
    "shows the ZIP output hint for %s blocks with a ZIP URL",
    (fileType) => {
      mockNodeFixtures.set("fp1", {
        id: "fp1",
        type: "fileParser",
        data: {
          ...baseFileParserData,
          fileType,
          fileUrl: "https://example.com/archive.zip",
        },
      });
      render(<FileParserBlockForm blockId="fp1" />);

      expect(
        screen.getByText(/Data Schema is ignored for ZIP archives/),
      ).toBeDefined();
    },
  );

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

  test("exposes the failure-mode control, flagged as inside a loop when nested in one", () => {
    mockNodeFixtures.set("loop1", {
      id: "loop1",
      type: "loop",
      data: { nextLoopOnFailure: false },
    });
    mockNodeFixtures.set("fp1", {
      id: "fp1",
      type: "fileParser",
      parentId: "loop1",
      data: baseFileParserData,
    });
    render(<FileParserBlockForm blockId="fp1" />);
    openAdvancedSettings();

    const options = screen.getByTestId("block-execution-options");
    expect(options.getAttribute("data-block-type")).toBe("fileParser");
    expect(options.getAttribute("data-inside-loop")).toBe("true");
    expect(options.getAttribute("data-parent-skips")).toBe("false");
    expect(options.getAttribute("data-next-loop")).toBe("false");
  });

  test("selecting skip-to-next-iteration writes nextLoopOnFailure so a bad file is skipped", () => {
    mockNodeFixtures.set("loop1", {
      id: "loop1",
      type: "loop",
      data: { nextLoopOnFailure: false },
    });
    mockNodeFixtures.set("fp1", {
      id: "fp1",
      type: "fileParser",
      parentId: "loop1",
      data: baseFileParserData,
    });
    render(<FileParserBlockForm blockId="fp1" />);
    openAdvancedSettings();

    fireEvent.click(screen.getByTestId("next-loop-on-failure-toggle"));

    expect(mockUpdateNodeData).toHaveBeenCalledWith("fp1", {
      continueOnFailure: false,
    });
    expect(mockUpdateNodeData).toHaveBeenCalledWith("fp1", {
      nextLoopOnFailure: true,
    });
  });

  test("reports the parent loop's skip-on-fail setting so the control can hide 'Stop the loop'", () => {
    mockNodeFixtures.set("loop1", {
      id: "loop1",
      type: "loop",
      data: { nextLoopOnFailure: true },
    });
    mockNodeFixtures.set("fp1", {
      id: "fp1",
      type: "fileParser",
      parentId: "loop1",
      data: baseFileParserData,
    });
    render(<FileParserBlockForm blockId="fp1" />);
    openAdvancedSettings();

    expect(
      screen
        .getByTestId("block-execution-options")
        .getAttribute("data-parent-skips"),
    ).toBe("true");
  });

  test("a parser outside a loop is not flagged as inside one", () => {
    mockNodeFixtures.set("fp1", {
      id: "fp1",
      type: "fileParser",
      data: baseFileParserData,
    });
    render(<FileParserBlockForm blockId="fp1" />);
    openAdvancedSettings();

    expect(
      screen
        .getByTestId("block-execution-options")
        .getAttribute("data-inside-loop"),
    ).toBe("false");
  });

  test("failure-mode edits do not reach the node when the block is read-only", () => {
    mockNodeFixtures.set("fp1", {
      id: "fp1",
      type: "fileParser",
      data: { ...baseFileParserData, editable: false },
    });
    render(<FileParserBlockForm blockId="fp1" />);
    openAdvancedSettings();

    fireEvent.click(screen.getByTestId("next-loop-on-failure-toggle"));

    expect(mockUpdateNodeData).not.toHaveBeenCalled();
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
