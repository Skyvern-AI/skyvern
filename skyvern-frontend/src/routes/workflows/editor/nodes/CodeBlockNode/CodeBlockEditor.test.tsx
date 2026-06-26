// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { WorkflowScopeContext } from "@/routes/workflows/editor/WorkflowScopeContext";

import { CodeBlockEditor } from "./CodeBlockEditor";
import { codeBlockNodeDefaultData, type CodeBlockNodeData } from "./types";

const baseData: CodeBlockNodeData = {
  debuggable: true,
  editable: true,
  label: "code_block",
  code: "print(1)",
  continueOnFailure: false,
  parameterKeys: [],
  prompt: null,
  steps: null,
  dataSchema: "null",
  model: null,
};

const node = {
  id: "cb1",
  type: "codeBlock",
  data: { ...baseData },
};

const updateNodeData = vi.fn();
let codeBlockAccess = true;

vi.mock("@xyflow/react", () => ({
  useReactFlow: () => ({ getNode: () => node, updateNodeData }),
}));

vi.mock("@/hooks/useFeatureFlag", () => ({
  useFeatureFlag: (flag: string) =>
    flag === "CODE_BLOCK_ACCESS" ? codeBlockAccess : undefined,
}));

vi.mock("..", () => ({
  isWorkflowBlockNode: () => true,
}));

vi.mock("@/components/WorkflowBlockInputSet", () => ({
  WorkflowBlockInputSet: () => null,
}));

vi.mock(
  "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup",
  () => ({
    WorkflowDataSchemaInputGroup: () => (
      <div data-testid="data-schema-input-group" />
    ),
  }),
);

vi.mock("@/components/WorkflowBlockInputTextarea", () => ({
  WorkflowBlockInputTextarea: ({
    value,
    onChange,
  }: {
    value?: string;
    onChange: (value: string) => void;
  }) => (
    <textarea
      data-testid="block-input-textarea"
      value={value ?? ""}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
}));

vi.mock("@/routes/workflows/components/CodeEditor", () => ({
  CodeEditor: ({
    readOnly,
    extraExtensions,
  }: {
    readOnly?: boolean;
    extraExtensions?: Array<unknown>;
  }) => (
    <div
      data-testid="code-editor"
      data-readonly={String(Boolean(readOnly))}
      data-extension-count={String(extraExtensions?.length ?? 0)}
    />
  ),
}));

beforeEach(() => {
  node.data = { ...baseData };
  updateNodeData.mockClear();
  codeBlockAccess = true;
});

afterEach(cleanup);

function renderEditor(readOnly: boolean = false) {
  return render(
    <WorkflowScopeContext.Provider value={{ workflowId: "w", readOnly }}>
      <CodeBlockEditor blockId="cb1" />
    </WorkflowScopeContext.Provider>,
  );
}

function switchToCode() {
  fireEvent.click(screen.getByRole("button", { name: /Code/ }));
}

const codeFirstData: Partial<CodeBlockNodeData> = {
  prompt: "Open {{ url }}",
  steps: [{ description: "Open the page", action_type: "goto_url" }],
};

describe("CodeBlockEditor in a read-only scope", () => {
  test("keeps the code editor editable in the live editor scope", () => {
    renderEditor(false);

    expect(
      screen.getByTestId("code-editor").getAttribute("data-readonly"),
    ).toBe("false");
  });

  // CodeMirror buffers edits locally, so the displayed historical code must be read-only here.
  test("renders the code editor read-only in a read-only comparison scope", () => {
    renderEditor(true);

    expect(
      screen.getByTestId("code-editor").getAttribute("data-readonly"),
    ).toBe("true");
  });
});

test("wires the jinja highlight into the code editor", () => {
  renderEditor();

  // jinjaHighlight contributes 2 extensions (plugin + theme).
  expect(
    screen.getByTestId("code-editor").getAttribute("data-extension-count"),
  ).toBe("2");
});

describe("CodeBlockEditor for a code-first block", () => {
  test("defaults to the plain view: goal and steps, no inputs or code editor", () => {
    node.data = { ...baseData, ...codeFirstData };
    renderEditor();

    expect(screen.getByText("Goal")).toBeTruthy();
    expect(screen.getByText("Open the page")).toBeTruthy();
    // The readable action label is the per-step subtitle.
    expect(screen.getByText("Goto URL")).toBeTruthy();
    // Inputs and the code editor live in the code view, not the plain view.
    expect(screen.queryByText("Inputs")).toBeNull();
    expect(screen.queryByTestId("code-editor")).toBeNull();
  });

  test("exposes the inputs selector and code panel in the code view", () => {
    node.data = { ...baseData, ...codeFirstData };
    renderEditor();
    switchToCode();

    expect(screen.getByText("Inputs")).toBeTruthy();
    expect(screen.getByText("Code Input")).toBeTruthy();
    expect(screen.getByTestId("code-editor")).toBeTruthy();
    // The goal lives in the plain view only.
    expect(screen.queryByText("Goal")).toBeNull();
  });

  test("uses the parameter-autocomplete textarea for the goal", () => {
    node.data = { ...baseData, ...codeFirstData };
    renderEditor();

    const textareas = screen.getAllByTestId<HTMLTextAreaElement>(
      "block-input-textarea",
    );
    expect(textareas.map((textarea) => textarea.value)).toEqual([
      "Open {{ url }}",
    ]);
  });

  test("persists goal edits to the node data", () => {
    node.data = { ...baseData, ...codeFirstData };
    renderEditor();

    const [goalTextarea] = screen.getAllByTestId("block-input-textarea");
    fireEvent.change(goalTextarea!, { target: { value: "Open {{ link }}" } });

    expect(updateNodeData).toHaveBeenCalledWith("cb1", {
      prompt: "Open {{ link }}",
    });
  });

  test("collapses and expands the step list in the code view", () => {
    node.data = { ...baseData, ...codeFirstData };
    renderEditor();
    switchToCode();

    const toggle = () => screen.getByRole("button", { name: /Steps \(1\)/ });
    expect(screen.getByText("Open the page")).toBeTruthy();
    expect(toggle().getAttribute("aria-expanded")).toBe("true");

    fireEvent.click(toggle());
    expect(screen.queryByText("Open the page")).toBeNull();
    expect(toggle().getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(toggle());
    expect(screen.getByText("Open the page")).toBeTruthy();
    expect(toggle().getAttribute("aria-expanded")).toBe("true");
  });

  test("omits the code-view step list when the block has no steps", () => {
    node.data = { ...baseData, ...codeFirstData, steps: [] };
    renderEditor();
    switchToCode();

    expect(screen.getByText("Code Input")).toBeTruthy();
    expect(screen.queryByText(/Steps \(/)).toBeNull();
  });
});

describe("CodeBlockEditor step-to-code highlighting", () => {
  const steppedData: Partial<CodeBlockNodeData> = {
    prompt: "Search and open",
    steps: [
      {
        description: "Open the page",
        action_type: "goto_url",
        line_start: 2,
        line_end: 3,
      },
      {
        description: "Read the title",
        action_type: "extract",
        line_start: 5,
        line_end: 5,
      },
    ],
  };

  test("shows each step's line range in the code view", () => {
    node.data = { ...baseData, ...steppedData };
    renderEditor();
    switchToCode();

    expect(screen.getByText("L2-3")).toBeTruthy();
    expect(screen.getByText("L5")).toBeTruthy();
  });

  test("highlights the clicked step's lines and toggles off", () => {
    node.data = { ...baseData, ...steppedData };
    renderEditor();
    switchToCode();

    const editor = () => screen.getByTestId("code-editor");
    // Baseline: jinja only (2 extensions), no active step.
    expect(editor().getAttribute("data-extension-count")).toBe("2");

    const stepButton = screen.getByRole("button", { name: /Open the page/ });
    fireEvent.click(stepButton);
    // jinja (2) + lineHighlight field + theme (2) = 4.
    expect(editor().getAttribute("data-extension-count")).toBe("4");
    expect(stepButton.getAttribute("aria-pressed")).toBe("true");

    fireEvent.click(stepButton);
    expect(editor().getAttribute("data-extension-count")).toBe("2");
    expect(stepButton.getAttribute("aria-pressed")).toBe("false");
  });
});

describe("CodeBlockEditor for a legacy block", () => {
  test("renders the inputs and code sections when goal is null", () => {
    renderEditor();

    expect(screen.getByText("Inputs")).toBeTruthy();
    expect(screen.getByText("Code Input")).toBeTruthy();
    expect(screen.queryByText("Goal")).toBeNull();
    expect(screen.queryByText("View")).toBeNull();
    expect(screen.queryAllByTestId("block-input-textarea")).toHaveLength(0);
  });

  test("treats a block missing the goal field entirely as legacy", () => {
    // Simulates pre-migration node data where the field is absent, not null.
    node.data = {
      ...baseData,
      prompt: undefined,
    } as unknown as CodeBlockNodeData;
    renderEditor();

    expect(screen.getByText("Code Input")).toBeTruthy();
    expect(screen.getByText("Inputs")).toBeTruthy();
    expect(screen.queryByText("Goal")).toBeNull();
  });
});

describe("CodeBlockEditor for a newly-added block from the node adder", () => {
  test("renders the code-first plain view because the default goal is non-null", () => {
    node.data = { ...codeBlockNodeDefaultData, label: "code_block" };
    renderEditor();

    expect(screen.getByText("Goal")).toBeTruthy();
    expect(screen.getByText("View")).toBeTruthy();
    // Steps are copilot-authored annotations, so a hand-added block has none yet.
    expect(screen.getByText(/No steps yet/)).toBeTruthy();
  });
});

describe("CodeBlockEditor view toggle", () => {
  test("switches between the plain and code views", () => {
    node.data = { ...baseData, ...codeFirstData };
    renderEditor(false);

    // Plain by default.
    expect(screen.getByTitle("Open the page")).toBeTruthy();
    expect(screen.queryByTestId("code-editor")).toBeNull();

    switchToCode();
    expect(screen.getByTestId("code-editor")).toBeTruthy();
    expect(screen.getByText("Inputs")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Plain" }));
    expect(screen.queryByTestId("code-editor")).toBeNull();
    expect(screen.getByText("Goal")).toBeTruthy();
  });

  test("shows an empty hint in the plain view when there are no steps", () => {
    node.data = { ...baseData, ...codeFirstData, steps: [] };
    renderEditor(false);

    expect(screen.getByText("Goal")).toBeTruthy();
    expect(screen.getByText(/No steps yet/)).toBeTruthy();
    expect(screen.queryByTestId("code-editor")).toBeNull();
  });
});

describe("CodeBlockEditor without code-first access", () => {
  test("renders the legacy code layout even when the block carries a goal", () => {
    codeBlockAccess = false;
    node.data = { ...baseData, ...codeFirstData };
    renderEditor();

    expect(screen.getByText("Code Input")).toBeTruthy();
    expect(screen.queryByText("Goal")).toBeNull();
    expect(screen.queryByText("View")).toBeNull();
  });
});

describe("CodeBlockEditor generate gating", () => {
  test("enables regenerate in the editable live scope", () => {
    node.data = { ...baseData, ...codeFirstData };
    renderEditor(false);

    const button = screen.getByRole<HTMLButtonElement>("button", {
      name: "Regenerate block",
    });
    expect(button.disabled).toBe(false);
  });

  test("disables regenerate in a read-only comparison scope", () => {
    node.data = { ...baseData, ...codeFirstData };
    renderEditor(true);

    const button = screen.getByRole<HTMLButtonElement>("button", {
      name: "Regenerate block",
    });
    expect(button.disabled).toBe(true);
  });

  test("disables generate when the node is not editable", () => {
    node.data = { ...baseData, prompt: "Do a thing", steps: null };
    node.data.editable = false;
    renderEditor(false);

    const button = screen.getByRole<HTMLButtonElement>("button", {
      name: "Generate block",
    });
    expect(button.disabled).toBe(true);
  });
});
