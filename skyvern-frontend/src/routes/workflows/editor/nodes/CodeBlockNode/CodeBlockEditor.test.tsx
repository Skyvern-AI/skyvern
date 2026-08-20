// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { WorkflowScopeContext } from "@/routes/workflows/editor/WorkflowScopeContext";
import {
  getWorkflowBlocks,
  getWorkflowErrors,
} from "@/routes/workflows/editor/workflowEditorUtils";

import { CodeBlockEditor } from "./CodeBlockEditor";
import { codeBlockNodeDefaultData, type CodeBlockNodeData } from "./types";
import { navigationNodeDefaultData } from "../NavigationNode/types";
import type { WorkflowStartNodeData } from "../StartNode/types";

const baseData: CodeBlockNodeData = {
  debuggable: true,
  editable: true,
  label: "code_block",
  code: "print(1)",
  continueOnFailure: false,
  parameterKeys: [],
  errorCodeMapping: "null",
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
let workflowErrorCodeMapping: Record<string, string> | null = null;

vi.mock("@xyflow/react", () => ({
  useNodes: () => [
    {
      id: "start",
      type: "start",
      data: { errorCodeMapping: workflowErrorCodeMapping },
    },
    node,
  ],
  useReactFlow: () => ({
    getNode: () => node,
    updateNodeData,
  }),
}));

vi.mock("@/hooks/useFeatureFlag", () => ({
  useFeatureFlag: (flag: string) =>
    flag === "CODE_BLOCK_ACCESS" ? codeBlockAccess : undefined,
}));

vi.mock("..", () => ({
  errorMappingExampleValue: {
    sample_invalid_credentials: "if the credentials are incorrect, terminate",
  },
  isWorkflowBlockNode: () => true,
}));

vi.mock("@/routes/workflows/editor/ErrorCodeMappingEditor", () => ({
  ErrorCodeMappingEditor: ({
    value,
    onChange,
  }: {
    value: string;
    onChange: (value: string) => void;
  }) => (
    <textarea
      data-testid="error-code-mapping-editor"
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  ),
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
  workflowErrorCodeMapping = null;
});

afterEach(cleanup);

describe("save-time error code mapping validation", () => {
  const createCodeBlock = (
    errorCodeMapping: string,
    code: string = baseData.code,
  ) => ({
    id: "code-1",
    type: "codeBlock" as const,
    position: { x: 0, y: 0 },
    data: {
      ...baseData,
      label: "code_block",
      errorCodeMapping,
      code,
    },
  });

  const createWorkflowStart = (
    errorCodeMapping: Record<string, string> | null,
  ) => ({
    id: "start",
    type: "start" as const,
    position: { x: 0, y: 0 },
    data: {
      withWorkflowSettings: true as const,
      errorCodeMapping,
    } as WorkflowStartNodeData,
  });

  test("rejects malformed JSON with the same error as Navigation", () => {
    const malformedMapping = "{not json";
    const codeErrors = getWorkflowErrors([createCodeBlock(malformedMapping)]);
    const navigationErrors = getWorkflowErrors([
      {
        id: "navigation-1",
        type: "navigation",
        position: { x: 0, y: 0 },
        data: {
          ...navigationNodeDefaultData,
          label: "code_block",
          navigationGoal: "Navigate",
          errorCodeMapping: malformedMapping,
        },
      },
    ]);

    expect(codeErrors).toEqual(navigationErrors);
    expect(codeErrors).toHaveLength(1);
    expect(codeErrors[0]).toContain("code_block");
    expect(codeErrors[0]).toContain("Error messages are not valid JSON");
  });

  test.each(["null", ""])(
    "allows a cleared mapping (%j) and serializes it to null",
    (errorCodeMapping) => {
      const codeBlock = createCodeBlock(errorCodeMapping);

      expect(getWorkflowErrors([codeBlock])).toEqual([]);
      expect(getWorkflowBlocks([codeBlock], [])[0]).toMatchObject({
        error_code_mapping: null,
      });
    },
  );

  test("allows a valid mapping and round-trips it unchanged", () => {
    const mapping = { FAILED: "the code failed" };
    const codeBlock = createCodeBlock(JSON.stringify(mapping));

    expect(getWorkflowErrors([codeBlock])).toEqual([]);
    expect(getWorkflowBlocks([codeBlock], [])[0]).toMatchObject({
      error_code_mapping: mapping,
    });
  });

  test("rejects an undeclared raised error code", () => {
    const errors = getWorkflowErrors([
      createCodeBlock("null", "raise ErrorCode('NEW_CODE', 'reason')"),
    ]);

    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain("code_block");
    expect(errors[0]).toContain("NEW_CODE");
  });

  test("allows a declared but unused error code", () => {
    expect(
      getWorkflowErrors([
        createCodeBlock(JSON.stringify({ UNUSED: "draft description" })),
      ]),
    ).toEqual([]);
  });

  test("rejects malformed ErrorCode usage", () => {
    const errors = getWorkflowErrors([
      createCodeBlock("null", "raise ErrorCode(code, 'reason')"),
    ]);

    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain("code_block");
    expect(errors[0]).toContain("line 1");
  });

  test("allows a declared and raised error code", () => {
    expect(
      getWorkflowErrors([
        createCodeBlock(
          JSON.stringify({ DECLARED: "known failure" }),
          "raise ErrorCode('DECLARED', 'reason')",
        ),
      ]),
    ).toEqual([]);
  });

  test("allows a raise declared in the workflow-level manifest", () => {
    expect(
      getWorkflowErrors([
        createWorkflowStart({ INHERITED: "workflow failure" }),
        createCodeBlock("null", "raise ErrorCode('INHERITED', 'reason')"),
      ]),
    ).toEqual([]);
  });
});

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

describe("CodeBlockEditor error messages", () => {
  const sampleMapping = JSON.stringify(
    {
      sample_invalid_credentials: "if the credentials are incorrect, terminate",
    },
    null,
    2,
  );

  const expectToRenderBefore = (first: HTMLElement, second: HTMLElement) => {
    expect(
      first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  };

  test("renders error messages below the code in the legacy layout", () => {
    renderEditor();

    expectToRenderBefore(
      screen.getByText("Code Input"),
      screen.getByText("Error Messages"),
    );
  });

  test("renders error messages below the code in the code-first code view", () => {
    node.data = { ...baseData, ...codeFirstData };
    renderEditor();
    switchToCode();

    expectToRenderBefore(
      screen.getByText("Code Input"),
      screen.getByText("Error Messages"),
    );
  });

  test("renders error messages below the steps card in the code-first plain view", () => {
    node.data = { ...baseData, ...codeFirstData };
    renderEditor();

    expectToRenderBefore(
      screen.getByText("Open the page"),
      screen.getByText("Error Messages"),
    );
  });

  test("toggles the Navigation-compatible editor in the legacy layout", () => {
    renderEditor();
    expect(screen.getByText("Error Messages")).toBeTruthy();
    expect(screen.queryByTestId("error-code-mapping-editor")).toBeNull();

    fireEvent.click(screen.getByRole("switch"));
    expect(updateNodeData).toHaveBeenLastCalledWith("cb1", {
      errorCodeMapping: sampleMapping,
    });

    node.data = { ...baseData, errorCodeMapping: sampleMapping };
    cleanup();
    renderEditor();
    expect(screen.getByTestId("error-code-mapping-editor")).toBeTruthy();
    fireEvent.click(screen.getByRole("switch"));
    expect(updateNodeData).toHaveBeenLastCalledWith("cb1", {
      errorCodeMapping: "null",
    });
  });

  test("renders the editor in both code-first layouts", () => {
    node.data = {
      ...baseData,
      ...codeFirstData,
      errorCodeMapping: sampleMapping,
    };
    renderEditor();
    expect(screen.getByTestId("error-code-mapping-editor")).toBeTruthy();
    switchToCode();
    expect(screen.getByTestId("error-code-mapping-editor")).toBeTruthy();
  });

  test("shows effective-manifest advisory statuses without disabling Generate", () => {
    workflowErrorCodeMapping = {
      workflow_only: "workflow declaration",
      matched: "workflow value overridden by block",
    };
    node.data = {
      ...baseData,
      ...codeFirstData,
      code: [
        'raise ErrorCode("matched", "reason")',
        'raise ErrorCode("raised_only", "reason")',
        'raise ErrorCode("workflow_only", "reason")',
        "raise ErrorCode(dynamic_code, 'reason')",
      ].join("\n"),
      errorCodeMapping: JSON.stringify({
        matched: "block override",
        declared_only: "unused block entry",
      }),
    };
    renderEditor();

    expect(screen.getByText("matched — raised on line 1")).toBeTruthy();
    expect(screen.getByText("workflow_only — raised on line 3")).toBeTruthy();
    expect(
      screen.getByText("declared_only — declared, not raised"),
    ).toBeTruthy();
    expect(
      screen.getByText("raised_only — raised on line 2, not declared"),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Malformed/nonliteral ErrorCode raises (ErrorCode cannot be imported or aliased) — line 4",
      ),
    ).toBeTruthy();
    expect(
      screen.getByRole<HTMLButtonElement>("button", {
        name: "Regenerate block",
      }).disabled,
    ).toBe(false);
  });

  test("caps rendered synchronization diagnostics", () => {
    node.data = {
      ...baseData,
      ...codeFirstData,
      code: "return {'ok': True}",
      errorCodeMapping: JSON.stringify(
        Object.fromEntries(
          Array.from({ length: 55 }, (_, index) => [
            `unused_${index}`,
            `condition ${index}`,
          ]),
        ),
      ),
    };

    renderEditor();

    const status = screen.getByLabelText(
      "Error message synchronization status",
    );
    expect(status.querySelectorAll("li")).toHaveLength(51);
    expect(screen.getByText("+5 more")).toBeTruthy();
    expect(screen.queryByText("unused_54 — declared, not raised")).toBeNull();
  });

  test("caps line numbers inside the malformed diagnostic row", () => {
    node.data = {
      ...baseData,
      code: Array.from(
        { length: 25 },
        (_, index) => `raise ErrorCode(dynamic_${index}, 'reason')`,
      ).join("\n"),
    };

    renderEditor();

    const status = screen.getByLabelText(
      "Error message synchronization status",
    );
    const malformedRow = status.querySelector("li");
    expect(malformedRow?.textContent).toContain("1, 2, 3, 4, 5");
    expect(malformedRow?.textContent).toContain("20 … and 5 more");
    expect(malformedRow?.textContent).not.toContain("21, 22");
    expect(malformedRow?.textContent.length).toBeLessThan(250);
  });
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
