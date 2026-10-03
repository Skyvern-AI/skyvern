// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

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
  CodeEditor: ({ value, readOnly }: { value: string; readOnly?: boolean }) => (
    <div
      data-testid="schema-editor"
      data-readonly={readOnly ? "true" : "false"}
    >
      {value}
    </div>
  ),
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

function renderGroup(readOnly: boolean, workflowId: string | null = "w") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const onChange = vi.fn();
  const view = render(
    <QueryClientProvider client={client}>
      <WorkflowScopeContext.Provider value={{ workflowId, readOnly }}>
        <WorkflowDataSchemaInputGroup
          value={'{"type":"object"}'}
          onChange={onChange}
          suggestionContext={{}}
          exampleValue={{}}
        />
      </WorkflowScopeContext.Provider>
    </QueryClientProvider>,
  );
  return { ...view, onChange };
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
