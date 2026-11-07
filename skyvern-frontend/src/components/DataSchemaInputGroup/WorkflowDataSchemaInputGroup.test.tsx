// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { useDeferredLockedEdit } from "@/hooks/useDeferredLockedEdit";
import { TSON } from "@/util/tson";
import { WorkflowScopeContext } from "@/routes/workflows/editor/WorkflowScopeContext";
import {
  beginCopilotAcceptance,
  beginSaveTransaction,
  createYamlCommitOwner,
  finishCopilotAcceptance,
  finishSaveTransaction,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";

import { WorkflowDataSchemaInputGroup } from "./WorkflowDataSchemaInputGroup";

const { post } = vi.hoisted(() => ({ post: vi.fn() }));
const generatedSchema = {
  type: "object",
  properties: { result: { type: "string" } },
};
const generatedSchemaText = JSON.stringify(generatedSchema, null, 2);

vi.mock("@/api/AxiosClient", () => ({
  getClient: async () => ({ post }),
}));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => undefined,
}));

vi.mock("@/routes/workflows/components/CodeEditor", () => ({
  CodeEditor: ({
    value,
    onChange,
    readOnly,
  }: {
    value: string;
    onChange?: (value: string) => void;
    readOnly?: boolean;
  }) => {
    const edit = useDeferredLockedEdit({ value, onChange });
    return (
      <div
        data-testid="schema-editor"
        data-readonly={readOnly ? "true" : "false"}
      >
        <textarea
          aria-label="schema text"
          readOnly={readOnly}
          value={edit.value}
          onChange={(event) => edit.onChange(event.target.value)}
          onBlur={edit.onBlur}
        />
      </div>
    );
  },
}));

beforeEach(() => {
  post.mockReset().mockResolvedValue({ data: { output: generatedSchema } });
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
});

afterEach(() => {
  cleanup();
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
});

function renderGroup(
  readOnly: boolean,
  workflowId: string | null = "w",
  { value = '{"type":"object"}', hideEnableToggle = false } = {},
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const onChange = vi.fn();
  const view = render(
    <QueryClientProvider client={client}>
      <WorkflowScopeContext.Provider value={{ workflowId, readOnly }}>
        <WorkflowDataSchemaInputGroup
          value={value}
          onChange={onChange}
          suggestionContext={{}}
          exampleValue={{}}
          hideEnableToggle={hideEnableToggle}
        />
      </WorkflowScopeContext.Provider>
    </QueryClientProvider>,
  );
  return { ...view, onChange };
}

function StatefulGroup({
  initialValue,
  onChange,
  exampleValue,
}: {
  initialValue: string;
  onChange: (value: string) => void;
  exampleValue: Record<string, unknown>;
}) {
  const [value, setValue] = useState(initialValue);
  return (
    <WorkflowDataSchemaInputGroup
      value={value}
      onChange={(next) => {
        onChange(next);
        setValue(next);
      }}
      suggestionContext={{}}
      exampleValue={exampleValue}
    />
  );
}

function renderStatefulGroup(
  initialValue: string,
  exampleValue: Record<string, unknown> = {},
) {
  const onChange = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <WorkflowScopeContext.Provider
        value={{ workflowId: "w", readOnly: false }}
      >
        <StatefulGroup
          initialValue={initialValue}
          onChange={onChange}
          exampleValue={exampleValue}
        />
      </WorkflowScopeContext.Provider>
    </QueryClientProvider>,
  );
  return { onChange };
}

function lockEditor(kind: "save" | "copilot") {
  if (kind === "save") {
    const owner = createYamlCommitOwner("w");
    act(() => {
      useWorkflowYamlEditorStore.setState({ editorOwner: owner });
      expect(beginSaveTransaction(owner)).toBe(true);
    });
    return () => act(() => finishSaveTransaction(owner));
  }
  let token: symbol | null = null;
  act(() => {
    token = beginCopilotAcceptance();
  });
  expect(token).not.toBeNull();
  return () => act(() => finishCopilotAcceptance(token!));
}

function openGenerationPrompt() {
  fireEvent.click(screen.getByRole("button", { name: "Generate with AI" }));
  const prompt = screen.getByPlaceholderText(
    "Describe how you want your output formatted",
  );
  fireEvent.change(prompt, { target: { value: "Return a result string" } });
  const submit = prompt.parentElement!.querySelector("svg:last-child")!;
  return { prompt, submit };
}

async function generateSchema() {
  const { submit } = openGenerationPrompt();
  fireEvent.click(submit);
  return screen.findByRole("dialog");
}

describe("WorkflowDataSchemaInputGroup in a read-only scope", () => {
  test("exposes the schema controls in the live editor scope", () => {
    renderGroup(false);
    expect(screen.queryByText("Generate with AI")).not.toBeNull();
    expect(screen.getByRole("checkbox").hasAttribute("disabled")).toBe(false);
    expect(
      screen.getByTestId("schema-editor").getAttribute("data-readonly"),
    ).toBe("false");
  });

  // A comparison canvas must stay inert: the schema is visible but cannot be
  // edited, toggled, or sent to /suggest/data_schema.
  test("locks the schema and hides Generate with AI in a read-only comparison scope", () => {
    renderGroup(true);
    expect(screen.queryByText("Generate with AI")).toBeNull();
    expect(screen.getByRole("checkbox").hasAttribute("disabled")).toBe(true);
    expect(
      screen.getByTestId("schema-editor").getAttribute("data-readonly"),
    ).toBe("true");
  });
});

describe("WorkflowDataSchemaInputGroup without the enable toggle", () => {
  test("keeps the editor and Generate with AI when the schema text is null", async () => {
    renderGroup(false, "w", { value: "null", hideEnableToggle: true });
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.getByTestId("schema-editor").textContent).toBe("null");
    expect(await generateSchema()).not.toBeNull();
  });
});

describe.each(["save", "copilot"] as const)(
  "WorkflowDataSchemaInputGroup during a %s transaction",
  (kind) => {
    test("locks schema toggling and the Generate with AI entry point until release", () => {
      const release = lockEditor(kind);
      const { onChange } = renderGroup(false);
      const checkbox = screen.getByRole("checkbox");

      expect(checkbox.hasAttribute("disabled")).toBe(true);
      expect(
        screen.queryByRole("button", { name: "Generate with AI" }),
      ).toBeNull();
      fireEvent.click(checkbox);
      expect(onChange).not.toHaveBeenCalled();

      release();
      expect(checkbox.hasAttribute("disabled")).toBe(false);
      expect(
        screen
          .getByRole("button", { name: "Generate with AI" })
          .hasAttribute("disabled"),
      ).toBe(false);
      fireEvent.click(checkbox);
      expect(onChange).toHaveBeenCalledExactlyOnceWith("null");
    });

    test("disables an open AI prompt and prevents generation until release", async () => {
      renderGroup(false);
      const { prompt, submit } = openGenerationPrompt();
      const release = lockEditor(kind);

      expect(prompt.hasAttribute("disabled")).toBe(true);
      fireEvent.click(submit);
      await act(async () => {});
      expect(post).not.toHaveBeenCalled();

      release();
      expect(prompt.hasAttribute("disabled")).toBe(false);
      expect((prompt as HTMLTextAreaElement).value).toBe(
        "Return a result string",
      );
      fireEvent.click(submit);
      expect(await screen.findByRole("dialog")).not.toBeNull();
    });

    test("keeps the dialog and generated suggestion when Accept Changes is clicked while locked", async () => {
      const { onChange } = renderGroup(false);
      const dialog = await generateSchema();
      const accept = screen.getByRole("button", { name: "Accept Changes" });
      const release = lockEditor(kind);

      fireEvent.click(accept);
      expect(screen.queryByRole("dialog")).toBe(dialog);
      expect(
        screen.getByText(generatedSchemaText, { normalizer: (text) => text }),
      ).not.toBeNull();
      expect(onChange).not.toHaveBeenCalled();
      expect(accept.hasAttribute("disabled")).toBe(true);

      release();
      expect(accept.hasAttribute("disabled")).toBe(false);
      fireEvent.click(accept);
      expect(onChange).toHaveBeenCalledExactlyOnceWith(generatedSchemaText);
      expect(screen.queryByRole("dialog")).toBeNull();
    });

    test("keeps schema controls enabled without a workflow scope", async () => {
      lockEditor(kind);
      const { onChange } = renderGroup(false, null);

      expect(screen.getByRole("checkbox").hasAttribute("disabled")).toBe(false);
      expect(
        screen
          .getByRole("button", { name: "Generate with AI" })
          .hasAttribute("disabled"),
      ).toBe(false);
      const { prompt, submit } = openGenerationPrompt();
      expect(prompt.hasAttribute("disabled")).toBe(false);
      fireEvent.click(submit);
      await screen.findByRole("dialog");
      const accept = screen.getByRole("button", { name: "Accept Changes" });
      expect(accept.hasAttribute("disabled")).toBe(false);
      fireEvent.click(accept);
      expect(onChange).toHaveBeenCalledExactlyOnceWith(generatedSchemaText);
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  },
);

const listedSchema = JSON.stringify(
  { type: "object", properties: { total: { type: "number" } } },
  null,
  2,
);

function groupWith({ value, deferKey }: { value: string; deferKey?: string }) {
  return (
    <QueryClientProvider client={new QueryClient()}>
      <WorkflowScopeContext.Provider
        value={{ workflowId: "w", readOnly: false }}
      >
        <WorkflowDataSchemaInputGroup
          deferKey={deferKey}
          value={value}
          onChange={vi.fn()}
          suggestionContext={{}}
          exampleValue={{}}
        />
      </WorkflowScopeContext.Provider>
    </QueryClientProvider>
  );
}

function editorHidden() {
  return screen
    .getByTestId("schema-editor")
    .parentElement!.classList.contains("hidden");
}

describe("WorkflowDataSchemaInputGroup field list", () => {
  test("opens as fields and switches to the mounted editor and back", () => {
    renderGroup(false, "w", { value: listedSchema });
    expect(screen.getByTestId("schema-field-list").textContent).toContain(
      "total",
    );
    expect(editorHidden()).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Show JSON" }));
    expect(screen.queryByTestId("schema-field-list")).toBeNull();
    expect(editorHidden()).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Show fields" }));
    expect(screen.getByTestId("schema-field-list")).not.toBeNull();
  });

  test.each([
    ["invalid JSON", '{"type": "object",'],
    ["the default schema", '{"type":"object"}'],
    [
      "compact JSON that the template parser rejects",
      '{"type":"object","properties":{"a":{"type":"string"}}}',
    ],
  ])("shows only the editor for %s", (_, value) => {
    renderGroup(false, "w", { value });
    expect(screen.queryByTestId("schema-field-list")).toBeNull();
    expect(screen.queryByRole("button", { name: "Show fields" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Show JSON" })).toBeNull();
    expect(editorHidden()).toBe(false);
  });

  test("keeps the parse error ring and message on the editor for invalid JSON", () => {
    const invalid = '{"type": "object",';
    renderGroup(false, "w", { value: invalid });
    expect(
      screen.getByTestId("schema-editor").parentElement!.className,
    ).toContain("ring-red-500");
    const message = TSON.parse(invalid).error!;
    expect(message).not.toBe("");
    expect(screen.getByText(message).className).toContain("text-red-400");
  });

  test.each([
    ["the default schema", '{"type":"object"}'],
    ["invalid JSON", '{"type": "object",'],
  ])(
    "keeps the editor visible when %s is edited into a listable schema",
    (_, value) => {
      renderStatefulGroup(value);
      const input = screen.getByLabelText("schema text");
      fireEvent.change(input, { target: { value: listedSchema } });
      fireEvent.blur(input);

      expect(screen.queryByTestId("schema-field-list")).toBeNull();
      expect(editorHidden()).toBe(false);
      expect(
        screen.getByRole("button", { name: "Show fields" }),
      ).not.toBeNull();
    },
  );

  test("opens the editor when the schema is enabled with a listable example", () => {
    renderStatefulGroup("null", JSON.parse(listedSchema));
    fireEvent.click(screen.getByRole("checkbox"));

    expect(screen.queryByTestId("schema-field-list")).toBeNull();
    expect(editorHidden()).toBe(false);
    expect(screen.getByRole("button", { name: "Show fields" })).not.toBeNull();
  });

  test("follows external writes between unlistable and listable schemas", () => {
    const { rerender } = render(groupWith({ value: '{"type":"object"}' }));
    expect(editorHidden()).toBe(false);

    rerender(groupWith({ value: listedSchema }));
    expect(screen.getByTestId("schema-field-list")).not.toBeNull();
    expect(editorHidden()).toBe(true);

    const invalid = '{"type": "object",';
    rerender(groupWith({ value: invalid }));
    expect(screen.queryByTestId("schema-field-list")).toBeNull();
    expect(screen.queryByRole("button", { name: "Show fields" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Show JSON" })).toBeNull();
    expect(editorHidden()).toBe(false);
    expect(screen.getByText(TSON.parse(invalid).error!)).not.toBeNull();

    rerender(groupWith({ value: listedSchema }));
    expect(screen.getByTestId("schema-field-list")).not.toBeNull();
  });

  test("does not carry a JSON choice to another block in the same panel", () => {
    const { rerender } = render(
      groupWith({ value: listedSchema, deferKey: "a" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Show JSON" }));
    expect(editorHidden()).toBe(false);

    rerender(groupWith({ value: listedSchema, deferKey: "b" }));
    expect(screen.getByTestId("schema-field-list")).not.toBeNull();

    rerender(groupWith({ value: listedSchema, deferKey: "a" }));
    expect(editorHidden()).toBe(false);
  });

  test("toggles in a read-only scope and leaves the editor read-only", () => {
    renderGroup(true, "w", { value: listedSchema });
    fireEvent.click(screen.getByRole("button", { name: "Show JSON" }));
    expect(
      screen.getByTestId("schema-editor").getAttribute("data-readonly"),
    ).toBe("true");
    const fields = screen.getByRole("button", { name: "Show fields" });
    expect(fields.hasAttribute("disabled")).toBe(false);
    fireEvent.click(fields);
    expect(screen.getByTestId("schema-field-list")).not.toBeNull();
  });

  test("toggles while a save holds the lock and the editor still rejects edits", () => {
    const { onChange } = renderGroup(false, "w", { value: listedSchema });
    lockEditor("save");
    fireEvent.click(screen.getByRole("button", { name: "Show JSON" }));
    const input = screen.getByLabelText("schema text");
    fireEvent.change(input, { target: { value: "{}" } });
    fireEvent.blur(input);
    expect(onChange).not.toHaveBeenCalled();

    const fields = screen.getByRole("button", { name: "Show fields" });
    expect(fields.hasAttribute("disabled")).toBe(false);
    fireEvent.click(fields);
    expect(screen.getByTestId("schema-field-list").textContent).toContain(
      "total",
    );
  });

  test("never calls onChange when only the view is toggled", () => {
    const { onChange } = renderGroup(false, "w", { value: listedSchema });
    fireEvent.click(screen.getByRole("button", { name: "Show JSON" }));
    fireEvent.click(screen.getByRole("button", { name: "Show fields" }));
    fireEvent.click(screen.getByRole("button", { name: "Show JSON" }));
    expect(onChange).not.toHaveBeenCalled();
  });

  test("shows a field typed in JSON when Fields is clicked before the debounce fires", () => {
    const { onChange } = renderStatefulGroup(listedSchema);
    fireEvent.click(screen.getByRole("button", { name: "Show JSON" }));
    const typed = JSON.stringify(
      {
        type: "object",
        properties: { total: { type: "number" }, currency: { type: "string" } },
      },
      null,
      2,
    );
    fireEvent.change(screen.getByLabelText("schema text"), {
      target: { value: typed },
    });
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Show fields" }));
    expect(onChange).toHaveBeenLastCalledWith(typed);
    expect(screen.getByTestId("schema-field-list").textContent).toContain(
      "currency",
    );
  });

  test("shows the editor, its error and no toggle once typed JSON is invalid", () => {
    const { onChange } = renderStatefulGroup(listedSchema);
    fireEvent.click(screen.getByRole("button", { name: "Show JSON" }));
    const input = screen.getByLabelText("schema text");
    fireEvent.change(input, { target: { value: '{"type": "object",' } });
    fireEvent.blur(input);

    expect(onChange).toHaveBeenLastCalledWith('{"type": "object",');
    expect(screen.queryByRole("button", { name: "Show fields" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Show JSON" })).toBeNull();
    expect(editorHidden()).toBe(false);
    const frame = screen.getByTestId("schema-editor").parentElement!;
    expect(frame.className).toContain("ring-red-500");
    expect(frame.nextElementSibling!.className).toContain("text-red-400");
  });
});
