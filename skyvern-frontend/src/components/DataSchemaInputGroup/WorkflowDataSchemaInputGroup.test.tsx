// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { WorkflowScopeContext } from "@/routes/workflows/editor/WorkflowScopeContext";

import { WorkflowDataSchemaInputGroup } from "./WorkflowDataSchemaInputGroup";

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

afterEach(cleanup);

function renderGroup(readOnly: boolean) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <WorkflowScopeContext.Provider value={{ workflowId: "w", readOnly }}>
        <WorkflowDataSchemaInputGroup
          value={'{"type":"object"}'}
          onChange={() => {}}
          suggestionContext={{}}
          exampleValue={{}}
        />
      </WorkflowScopeContext.Provider>
    </QueryClientProvider>,
  );
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
