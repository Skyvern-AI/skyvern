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

function renderEditor(readOnly: boolean = false, view?: "plain" | "code") {
  return render(
    <WorkflowScopeContext.Provider value={{ workflowId: "w", readOnly }}>
      <CodeBlockEditor blockId="cb1" view={view} />
    </WorkflowScopeContext.Provider>,
  );
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
  test("renders goal, steps, and code panel in order", () => {
    node.data = { ...baseData, ...codeFirstData };
    const { container } = renderEditor();

    expect(screen.getByText("Goal")).toBeTruthy();
    expect(screen.getByText("Steps (1)")).toBeTruthy();
    expect(screen.getByText("Open the page")).toBeTruthy();
    expect(screen.getByText("goto_url")).toBeTruthy();

    const text = container.textContent ?? "";
    const order = ["Goal", "Steps (1)", "Code Input"].map((label) =>
      text.indexOf(label),
    );
    expect(order).toEqual([...order].sort((a, b) => a - b));
    expect(order.every((index) => index >= 0)).toBe(true);
  });

  test("renders the inputs selector alongside the code panel", () => {
    node.data = { ...baseData, ...codeFirstData };
    renderEditor();

    expect(screen.getByText("Inputs")).toBeTruthy();
    expect(screen.getByText("Code Input")).toBeTruthy();
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

  test("collapses and expands the step list", () => {
    node.data = { ...baseData, ...codeFirstData };
    renderEditor();

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

  test("omits the step list when the block has no steps", () => {
    node.data = { ...baseData, ...codeFirstData, steps: [] };
    renderEditor();

    expect(screen.getByText("Goal")).toBeTruthy();
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

  test("shows each step's line range", () => {
    node.data = { ...baseData, ...steppedData };
    renderEditor();

    expect(screen.getByText("L2-3")).toBeTruthy();
    expect(screen.getByText("L5")).toBeTruthy();
  });

  test("highlights the clicked step's lines and toggles off", () => {
    node.data = { ...baseData, ...steppedData };
    renderEditor();

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
    expect(screen.queryByText(/Steps \(/)).toBeNull();
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
  test("renders the code-first layout because the default goal is non-null", () => {
    node.data = { ...codeBlockNodeDefaultData, label: "code_block" };
    renderEditor();

    expect(screen.getByText("Goal")).toBeTruthy();
    // Steps are copilot-authored annotations, so a hand-added block has none yet.
    expect(screen.queryByText(/Steps \(/)).toBeNull();
  });
});

describe("CodeBlockEditor plain view", () => {
  test("shows goal and steps but not inputs or the code editor", () => {
    node.data = { ...baseData, ...codeFirstData };
    renderEditor(false, "plain");

    expect(screen.getByText("Goal")).toBeTruthy();
    expect(screen.getByText("What Skyvern will do")).toBeTruthy();
    expect(screen.getByText("Open the page")).toBeTruthy();
    // The readable action label is the per-step subtitle.
    expect(screen.getByText("Goto URL")).toBeTruthy();
    // Inputs and the code editor live outside the plain view now.
    expect(screen.queryByText("Inputs")).toBeNull();
    expect(screen.queryByTestId("code-editor")).toBeNull();
  });

  test("shows an empty hint when a code-first block has no steps", () => {
    node.data = { ...baseData, ...codeFirstData, steps: [] };
    renderEditor(false, "plain");

    expect(screen.getByText("What Skyvern will do")).toBeTruthy();
    expect(screen.getByText(/No steps yet/)).toBeTruthy();
    expect(screen.queryByTestId("code-editor")).toBeNull();
  });

  test("renders inputs and the code editor when the view is code", () => {
    node.data = { ...baseData, ...codeFirstData };
    renderEditor(false, "code");

    expect(screen.getByTestId("code-editor")).toBeTruthy();
    expect(screen.getByText("Inputs")).toBeTruthy();
    expect(screen.queryByText("Goal")).toBeNull();
    expect(screen.queryByText("What Skyvern will do")).toBeNull();
  });

  test("ignores the split view for a legacy block without a goal", () => {
    renderEditor(false, "plain");

    expect(screen.queryByText("What Skyvern will do")).toBeNull();
    expect(screen.getByText("Code Input")).toBeTruthy();
  });
});

describe("CodeBlockEditor without code-first access", () => {
  test("renders the legacy code layout even when the block carries a goal", () => {
    codeBlockAccess = false;
    node.data = { ...baseData, ...codeFirstData };
    renderEditor();

    expect(screen.getByText("Code Input")).toBeTruthy();
    expect(screen.queryByText("Goal")).toBeNull();
    expect(screen.queryByText(/Steps \(/)).toBeNull();
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
